//! Tauri commands for the proposal lifecycle and the extensions listing.
//!
//! Each command forwards to the live sidecar over loopback HTTP with the current port and
//! ephemeral token, and returns the sidecar's JSON body unchanged, so the frontend sees
//! the shapes defined by the Python side. Non-2xx responses become errors carrying the
//! sidecar's `detail` message.

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::State;

use crate::sidecar::{Sidecar, SidecarHandle};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(15);
/// Propose runs ruff and mypy, which can take a while on a cold cache.
const PREFLIGHT_TIMEOUT: Duration = Duration::from_secs(200);
const MAX_PROPOSAL_ID_LEN: usize = 64;

struct HttpReply {
    status: u16,
    body: String,
}

/// Rejects anything that is not a plain hex id before it is placed in a URL path.
fn validated_id(id: &str) -> Result<&str, String> {
    if !id.is_empty()
        && id.len() <= MAX_PROPOSAL_ID_LEN
        && id.chars().all(|c| c.is_ascii_hexdigit())
    {
        Ok(id)
    } else {
        Err("invalid proposal id".to_owned())
    }
}

/// Decodes an HTTP/1.1 chunked body.
fn dechunk(mut data: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    loop {
        let line_end = data
            .windows(2)
            .position(|w| w == b"\r\n")
            .ok_or("malformed chunked response")?;
        let size_line = std::str::from_utf8(&data[..line_end])
            .map_err(|_| "malformed chunk size".to_owned())?;
        let size_hex = size_line.split(';').next().unwrap_or("").trim();
        let size = usize::from_str_radix(size_hex, 16)
            .map_err(|_| "malformed chunk size".to_owned())?;
        data = &data[line_end + 2..];
        if size == 0 {
            return Ok(out);
        }
        if data.len() < size + 2 {
            return Err("truncated chunked response".to_owned());
        }
        out.extend_from_slice(&data[..size]);
        data = &data[size + 2..];
    }
}

fn parse_http(raw: &[u8]) -> Result<HttpReply, String> {
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or("malformed response from the Daisy backend")?;
    let head = String::from_utf8_lossy(&raw[..split]).into_owned();
    let body_bytes = &raw[split + 4..];
    let mut lines = head.lines();
    let status_line = lines.next().unwrap_or("");
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| format!("unexpected status line: {status_line}"))?;
    let chunked = lines.any(|line| {
        let lower = line.to_ascii_lowercase();
        lower.starts_with("transfer-encoding:") && lower.contains("chunked")
    });
    let body = if chunked {
        dechunk(body_bytes)?
    } else {
        body_bytes.to_vec()
    };
    Ok(HttpReply {
        status,
        body: String::from_utf8_lossy(&body).into_owned(),
    })
}

fn send(
    port: u16,
    token: &str,
    method: &str,
    path: &str,
    body: Option<&Value>,
    timeout: Duration,
) -> Result<HttpReply, String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT)
        .map_err(|error| format!("could not reach the Daisy backend: {error}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|()| stream.set_write_timeout(Some(timeout)))
        .map_err(|error| error.to_string())?;
    let payload = match body {
        Some(value) => serde_json::to_string(value).map_err(|error| error.to_string())?,
        None => String::new(),
    };
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nAccept: application/json\r\nConnection: close\r\n"
    );
    if body.is_some() {
        request.push_str(&format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            payload.len()
        ));
    }
    request.push_str("\r\n");
    request.push_str(&payload);
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("could not send request: {error}"))?;
    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .map_err(|error| format!("could not read response: {error}"))?;
    parse_http(&raw)
}

/// Calls the live sidecar and maps the outcome to the JSON body or a readable error.
fn forward(
    sidecar: &Sidecar,
    method: &str,
    path: &str,
    body: Option<&Value>,
    timeout: Duration,
) -> Result<Value, String> {
    let connection = sidecar.connection()?;
    let reply = send(
        connection.port,
        &connection.token,
        method,
        path,
        body,
        timeout,
    )?;
    let parsed: Value = serde_json::from_str(&reply.body).unwrap_or(Value::Null);
    if (200..300).contains(&reply.status) {
        return Ok(parsed);
    }
    let detail = match parsed.get("detail") {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(other) if !other.is_null() => other.to_string(),
        _ => format!("the Daisy backend returned HTTP {}", reply.status),
    };
    Err(detail)
}

/// Stages a set of workspace file changes and runs pre-flight checks. Nothing is written.
#[tauri::command]
pub async fn propose_proposal(
    handle: State<'_, SidecarHandle>,
    changes: BTreeMap<String, String>,
) -> Result<Value, String> {
    let sidecar = handle.current();
    let body = json!({ "changes": changes });
    tauri::async_runtime::spawn_blocking(move || {
        forward(
            &sidecar,
            "POST",
            "/proposals/propose",
            Some(&body),
            PREFLIGHT_TIMEOUT,
        )
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Records a named human's approval. Fails if pre-flight did not pass.
#[tauri::command]
pub async fn approve_proposal(
    handle: State<'_, SidecarHandle>,
    proposal_id: String,
    approver: String,
) -> Result<Value, String> {
    let id = validated_id(&proposal_id)?.to_owned();
    let sidecar = handle.current();
    let body = json!({ "approver": approver });
    tauri::async_runtime::spawn_blocking(move || {
        let path = format!("/proposals/{id}/approve");
        forward(&sidecar, "POST", &path, Some(&body), DEFAULT_TIMEOUT)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Records a named human's rejection. A rejected proposal can never be applied.
#[tauri::command]
pub async fn reject_proposal(
    handle: State<'_, SidecarHandle>,
    proposal_id: String,
    approver: String,
) -> Result<Value, String> {
    let id = validated_id(&proposal_id)?.to_owned();
    let sidecar = handle.current();
    let body = json!({ "approver": approver });
    tauri::async_runtime::spawn_blocking(move || {
        let path = format!("/proposals/{id}/reject");
        forward(&sidecar, "POST", &path, Some(&body), DEFAULT_TIMEOUT)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Snapshots the targets and writes an approved proposal. Refused unless it is APPROVED.
#[tauri::command]
pub async fn apply_proposal(
    handle: State<'_, SidecarHandle>,
    proposal_id: String,
) -> Result<Value, String> {
    let id = validated_id(&proposal_id)?.to_owned();
    let sidecar = handle.current();
    tauri::async_runtime::spawn_blocking(move || {
        let path = format!("/proposals/{id}/apply");
        forward(&sidecar, "POST", &path, None, DEFAULT_TIMEOUT)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Lists discovered extensions with their load status.
#[tauri::command]
pub async fn list_extensions(handle: State<'_, SidecarHandle>) -> Result<Value, String> {
    let sidecar = handle.current();
    tauri::async_runtime::spawn_blocking(move || {
        forward(&sidecar, "GET", "/extensions", None, DEFAULT_TIMEOUT)
    })
    .await
    .map_err(|error| error.to_string())?
}
