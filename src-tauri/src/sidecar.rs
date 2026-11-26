//! Python sidecar lifecycle: spawn, READY handshake, connection info, reload, and shutdown.

use std::env;
use std::ffi::OsString;
use std::io::{BufRead, BufReader};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::State;
use uuid::Uuid;

/// Dev/macOS location of the Python backend, resolved at compile time.
pub const BACKEND_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../backend");
const READY_PREFIX: &str = "READY:";
const READY_TIMEOUT: Duration = Duration::from_secs(15);
/// How long a terminated process group gets to exit on SIGTERM before SIGKILL.
const GRACEFUL_EXIT_TIMEOUT: Duration = Duration::from_secs(3);
const EXIT_POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Directories GUI-launched apps often miss from PATH (Finder inherits a minimal PATH).
const FALLBACK_BIN_DIRS: &[&str] = &[".local/bin", ".cargo/bin"];
const FALLBACK_ABSOLUTE_DIRS: &[&str] = &["/opt/homebrew/bin", "/usr/local/bin"];

#[derive(Default)]
struct SidecarState {
    port: Option<u16>,
    child: Option<Child>,
}

/// One running sidecar process with its own ephemeral token.
///
/// The process is spawned in its own process group so `uv` and the Python grandchild
/// it launches can be terminated together.
pub struct Sidecar {
    token: String,
    pid: u32,
    state: Mutex<SidecarState>,
    ready: Condvar,
}

/// Tauri managed state: the currently live sidecar, swappable on reload.
#[derive(Clone)]
pub struct SidecarHandle(Arc<Mutex<Arc<Sidecar>>>);

#[derive(Serialize)]
pub struct ConnectionInfo {
    pub port: u16,
    pub token: String,
}

/// Returns a fresh UUIDv4 (32 hex characters) used as the sidecar's bearer secret.
pub fn generate_token() -> String {
    Uuid::new_v4().simple().to_string()
}

/// Every directory to search for `uv`: the inherited PATH first, then standard install locations.
fn candidate_dirs() -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = env::var_os("PATH")
        .map(|path| env::split_paths(&path).collect())
        .unwrap_or_default();
    if let Some(home) = env::var_os("HOME").map(PathBuf::from) {
        dirs.extend(FALLBACK_BIN_DIRS.iter().map(|rel| home.join(rel)));
    }
    dirs.extend(FALLBACK_ABSOLUTE_DIRS.iter().map(PathBuf::from));
    dirs
}

/// Finds an executable `uv`, returning its absolute path or a clear error listing what was searched.
pub fn resolve_uv() -> std::io::Result<PathBuf> {
    let dirs = candidate_dirs();
    dirs.iter()
        .map(|dir| dir.join("uv"))
        .find(|candidate| candidate.is_file())
        .ok_or_else(|| {
            let searched = dirs
                .iter()
                .map(|dir| dir.display().to_string())
                .collect::<Vec<_>>()
                .join(", ");
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("could not find `uv` (install it from astral.sh/uv); searched: {searched}"),
            )
        })
}

/// PATH for the child process: the inherited PATH plus the fallback directories, so `uv` can find `python`.
pub fn child_path(uv: &Path) -> std::io::Result<OsString> {
    let mut dirs = candidate_dirs();
    if let Some(parent) = uv.parent() {
        dirs.insert(0, parent.to_path_buf());
    }
    env::join_paths(dirs).map_err(|error| std::io::Error::other(error.to_string()))
}

/// Sends `signal` to every process in the group `pgid`. Errors (group already gone) are ignored.
#[cfg(unix)]
fn signal_group(pgid: u32, signal: libc::c_int) {
    // SAFETY: killpg only sends a signal; it does not touch memory owned by Rust.
    unsafe {
        libc::killpg(pgid as libc::pid_t, signal);
    }
}

/// True while any process still belongs to the group `pgid`.
#[cfg(unix)]
fn group_alive(pgid: u32) -> bool {
    // SAFETY: signal 0 only checks for existence of the group.
    unsafe { libc::killpg(pgid as libc::pid_t, 0) == 0 }
}

impl SidecarHandle {
    pub fn new(sidecar: Arc<Sidecar>) -> Self {
        Self(Arc::new(Mutex::new(sidecar)))
    }

    /// The currently live sidecar.
    pub fn current(&self) -> Arc<Sidecar> {
        Arc::clone(&lock_slot(&self.0))
    }

    /// Shared slot, for moving into a blocking task.
    pub fn slot(&self) -> Arc<Mutex<Arc<Sidecar>>> {
        Arc::clone(&self.0)
    }
}

fn lock_slot(slot: &Mutex<Arc<Sidecar>>) -> MutexGuard<'_, Arc<Sidecar>> {
    slot.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Sidecar {
    /// Spawns `uv run python -m daisy.main --auth-token <token>` in its own process group.
    pub fn start(token: String) -> std::io::Result<Arc<Self>> {
        let uv = resolve_uv()?;
        let mut command = Command::new(&uv);
        command
            .args(["run", "python", "-m", "daisy.main", "--auth-token", &token])
            .current_dir(BACKEND_DIR)
            .env("PATH", child_path(&uv)?)
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn()?;
        let pid = child.id();
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| std::io::Error::other("sidecar stdout is not piped"))?;
        let sidecar = Arc::new(Self {
            token,
            pid,
            state: Mutex::new(SidecarState {
                port: None,
                child: Some(child),
            }),
            ready: Condvar::new(),
        });
        let reader = Arc::clone(&sidecar);
        thread::spawn(move || reader.read_stdout(stdout));
        Ok(sidecar)
    }

    fn lock(&self) -> MutexGuard<'_, SidecarState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Drains stdout for the process lifetime, recording the port from the READY line.
    fn read_stdout(&self, stdout: ChildStdout) {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let Some(raw_port) = line.strip_prefix(READY_PREFIX) else {
                continue;
            };
            if let Ok(port) = raw_port.trim().parse::<u16>() {
                self.lock().port = Some(port);
                self.ready.notify_all();
            }
        }
    }

    /// Blocks (up to `READY_TIMEOUT`) until the sidecar reports its port.
    pub fn connection(&self) -> Result<ConnectionInfo, String> {
        let (state, _timeout) = match self.ready.wait_timeout_while(
            self.lock(),
            READY_TIMEOUT,
            |s| s.port.is_none(),
        ) {
            Ok(result) => result,
            Err(poisoned) => poisoned.into_inner(),
        };
        state
            .port
            .map(|port| ConnectionInfo {
                port,
                token: self.token.clone(),
            })
            .ok_or_else(|| "Daisy backend did not report READY in time".to_owned())
    }

    /// Terminates the sidecar and its whole process group. Safe to call more than once.
    ///
    /// On Unix, SIGTERM goes to the group first so `uv` and its Python child exit cleanly;
    /// any member still alive after `GRACEFUL_EXIT_TIMEOUT` is sent SIGKILL.
    pub fn shutdown(&self) {
        let child = self.lock().child.take();
        let Some(mut child) = child else {
            return;
        };
        #[cfg(unix)]
        {
            let pgid = self.pid;
            signal_group(pgid, libc::SIGTERM);
            let deadline = Instant::now() + GRACEFUL_EXIT_TIMEOUT;
            loop {
                // Reap the leader so its zombie does not keep the group looking alive.
                let _ = child.try_wait();
                if !group_alive(pgid) {
                    break;
                }
                if Instant::now() >= deadline {
                    signal_group(pgid, libc::SIGKILL);
                    break;
                }
                thread::sleep(EXIT_POLL_INTERVAL);
            }
        }
        #[cfg(not(unix))]
        {
            let _ = child.kill();
        }
        let _ = child.wait();
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// Replaces the live sidecar with a fresh one and waits for its READY port.
///
/// The slot lock is held only for the swap itself. Shutdown of the old process and the
/// READY wait on the new one both run with the lock released, so `get_sidecar_connection`
/// and other slot readers are never blocked for the duration of a reload.
pub fn reload_blocking(slot: &Mutex<Arc<Sidecar>>) -> Result<ConnectionInfo, String> {
    let previous = Arc::clone(&lock_slot(slot));
    previous.shutdown();
    let next = Sidecar::start(generate_token()).map_err(|error| error.to_string())?;
    *lock_slot(slot) = Arc::clone(&next);
    next.connection()
}

/// Returns the loopback port and ephemeral token the frontend needs to call the sidecar.
#[tauri::command]
pub async fn get_sidecar_connection(
    handle: State<'_, SidecarHandle>,
) -> Result<ConnectionInfo, String> {
    let sidecar = handle.current();
    tauri::async_runtime::spawn_blocking(move || sidecar.connection())
        .await
        .map_err(|error| error.to_string())?
}

/// Restarts the sidecar with a new token and returns its new port and token.
///
/// The old process group is terminated before the new one starts, so the old and new
/// backends never run at the same time.
#[tauri::command]
pub async fn reload_sidecar(handle: State<'_, SidecarHandle>) -> Result<ConnectionInfo, String> {
    let slot = handle.slot();
    tauri::async_runtime::spawn_blocking(move || reload_blocking(&slot))
        .await
        .map_err(|error| error.to_string())?
}
