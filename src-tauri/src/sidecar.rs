//! Python sidecar lifecycle: spawn, READY handshake, connection info, and shutdown.

use std::io::{BufRead, BufReader};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::Duration;

use serde::Serialize;
use tauri::State;
use uuid::Uuid;

/// Dev/macOS location of the Python backend, resolved at compile time.
const BACKEND_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../backend");
const READY_PREFIX: &str = "READY:";
const READY_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Default)]
struct SidecarState {
    port: Option<u16>,
    child: Option<Child>,
}

/// Owns the Python sidecar process; killed on drop or explicit shutdown.
pub struct Sidecar {
    token: String,
    state: Mutex<SidecarState>,
    ready: Condvar,
}

#[derive(Serialize)]
pub struct ConnectionInfo {
    pub port: u16,
    pub token: String,
}

/// Returns a fresh 32-character hex secret for the sidecar's bearer auth.
pub fn generate_token() -> String {
    Uuid::new_v4().simple().to_string()
}

impl Sidecar {
    /// Spawns `uv run python -m daisy.main --auth-token <token>` and starts watching its stdout.
    pub fn start(token: String) -> std::io::Result<Arc<Self>> {
        let mut child = Command::new("uv")
            .args(["run", "python", "-m", "daisy.main", "--auth-token", &token])
            .current_dir(BACKEND_DIR)
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| std::io::Error::other("sidecar stdout is not piped"))?;
        let sidecar = Arc::new(Self {
            token,
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

    /// Kills the sidecar process if it is still running. Safe to call more than once.
    pub fn shutdown(&self) {
        let child = self.lock().child.take();
        if let Some(mut child) = child {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// Returns the loopback port and ephemeral token the frontend needs to call the sidecar.
#[tauri::command]
pub async fn get_sidecar_connection(
    sidecar: State<'_, Arc<Sidecar>>,
) -> Result<ConnectionInfo, String> {
    let sidecar = sidecar.inner().clone();
    tauri::async_runtime::spawn_blocking(move || sidecar.connection())
        .await
        .map_err(|error| error.to_string())?
}
