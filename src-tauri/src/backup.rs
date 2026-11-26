//! Snapshot and rollback orchestration plus the sidecar health probe.
//!
//! Backups and rollbacks run the Python `daisy.backup` CLI through `uv`, so the same
//! workspace and path rules apply whether the call came from Rust or from Python.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::State;

use crate::sidecar::{resolve_uv, child_path, SidecarHandle, BACKEND_DIR};

/// Hard limit for a single health probe. A slower response counts as a failure.
pub const HEALTH_TIMEOUT: Duration = Duration::from_millis(3000);

#[derive(Serialize, Clone, Debug, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct HealthReport {
    pub ok: bool,
    pub detail: String,
}

#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct RollbackOutcome {
    pub snapshot_id: String,
    pub port: u16,
    pub token: String,
}

/// Probes `GET /health` on the loopback port with a strict timeout.
///
/// Any of the following counts as a failure: connection refused, a timeout past
/// `HEALTH_TIMEOUT`, a status other than 200, or a body that is not JSON with a
/// `status` of "ok". The probe uses plain HTTP over TCP so it has no extra dependency.
pub fn probe_health(port: u16) -> HealthReport {
    let started = Instant::now();
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = match TcpStream::connect_timeout(&addr, HEALTH_TIMEOUT) {
        Ok(stream) => stream,
        Err(error) => {
            return HealthReport {
                ok: false,
                detail: format!("connection failed: {error}"),
            }
        }
    };
    let remaining = HEALTH_TIMEOUT.saturating_sub(started.elapsed());
    if remaining.is_zero() {
        return HealthReport {
            ok: false,
            detail: "timed out after 3.0s".to_owned(),
        };
    }
    let _ = stream.set_read_timeout(Some(remaining));
    let _ = stream.set_write_timeout(Some(remaining));
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return HealthReport {
            ok: false,
            detail: "could not send health request".to_owned(),
        };
    }
    let mut response = String::new();
    if let Err(error) = stream.read_to_string(&mut response) {
        let reason = if matches!(error.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) {
            "timed out after 3.0s".to_owned()
        } else {
            format!("read failed: {error}")
        };
        return HealthReport { ok: false, detail: reason };
    }
    if started.elapsed() > HEALTH_TIMEOUT {
        return HealthReport {
            ok: false,
            detail: "timed out after 3.0s".to_owned(),
        };
    }
    evaluate_response(&response)
}

/// Judges a raw HTTP response: status line must be 200 and the body must report `ok`.
fn evaluate_response(response: &str) -> HealthReport {
    let status_line = response.lines().next().unwrap_or("");
    if !status_line.starts_with("HTTP/1.1 200") && !status_line.starts_with("HTTP/1.0 200") {
        return HealthReport {
            ok: false,
            detail: format!("unexpected status: {status_line}"),
        };
    }
    let body = response.split("\r\n\r\n").nth(1).unwrap_or("");
    let parsed: Result<serde_json::Value, _> = serde_json::from_str(body);
    match parsed {
        Ok(value) if value.get("status").and_then(|s| s.as_str()) == Some("ok") => HealthReport {
            ok: true,
            detail: "ok".to_owned(),
        },
        _ => HealthReport {
            ok: false,
            detail: "health body did not report ok".to_owned(),
        },
    }
}

/// Runs `uv run python -m daisy.backup <args>` in the backend directory and returns stdout.
fn run_backup_cli(args: &[&str]) -> Result<String, String> {
    let uv = resolve_uv().map_err(|error| error.to_string())?;
    let output = Command::new(&uv)
        .args(["run", "python", "-m", "daisy.backup"])
        .args(args)
        .current_dir(BACKEND_DIR)
        .env("PATH", child_path(&uv).map_err(|error| error.to_string())?)
        .stdin(Stdio::null())
        .output()
        .map_err(|error| format!("could not run backup CLI: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("backup CLI exited with {}", output.status)
        } else {
            stderr
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

/// Snapshots the given workspace files before a change. Paths are passed through to the
/// Python side, which rejects anything outside the workspace root.
#[tauri::command]
pub async fn create_backup(
    action_id: String,
    files: Vec<String>,
) -> Result<String, String> {
    if files.is_empty() {
        return Err("no files to back up".to_owned());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let mut args: Vec<&str> = vec!["create", "--action-id", &action_id, "--"];
        args.extend(files.iter().map(String::as_str));
        run_backup_cli(&args)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Probes the live sidecar's `/health` endpoint with the 3.0 second limit.
#[tauri::command]
pub async fn verify_health(handle: State<'_, SidecarHandle>) -> Result<HealthReport, String> {
    let port = handle
        .current()
        .connection()
        .map_err(|error| error)?
        .port;
    tauri::async_runtime::spawn_blocking(move || probe_health(port))
        .await
        .map_err(|error| error.to_string())
}

/// Restores the newest snapshot and restarts the sidecar so it runs the restored files.
///
/// Returns the restored snapshot id and the new connection. Fails without touching the
/// running sidecar if there is no snapshot.
#[tauri::command]
pub async fn rollback_latest(
    handle: State<'_, SidecarHandle>,
) -> Result<RollbackOutcome, String> {
    let slot = handle.slot();
    tauri::async_runtime::spawn_blocking(move || {
        let snapshot_id = run_backup_cli(&["rollback"])?;
        if snapshot_id.is_empty() {
            return Err("rollback returned no snapshot id".to_owned());
        }
        let connection = crate::sidecar::reload_blocking(&slot)?;
        Ok(RollbackOutcome {
            snapshot_id,
            port: connection.port,
            token: connection.token,
        })
    })
    .await
    .map_err(|error| error.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_a_200_with_ok_body() {
        let response = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"status\":\"ok\",\"timestamp\":\"2026-11-26T22:00:00+00:00\"}";
        assert!(evaluate_response(response).ok);
    }

    #[test]
    fn rejects_non_200() {
        let response = "HTTP/1.1 503 Service Unavailable\r\n\r\n{}";
        assert!(!evaluate_response(response).ok);
    }

    #[test]
    fn rejects_body_without_ok_status() {
        let response = "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"down\"}";
        assert!(!evaluate_response(response).ok);
    }
}
