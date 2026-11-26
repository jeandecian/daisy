mod sidecar;

use sidecar::{generate_token, get_sidecar_connection, reload_sidecar, Sidecar, SidecarHandle};
use tauri::{Manager, RunEvent};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let sidecar = Sidecar::start(generate_token())?;
            app.manage(SidecarHandle::new(sidecar));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_sidecar_connection,
            reload_sidecar
        ])
        .build(tauri::generate_context!())
        .expect("error while building Daisy");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(sidecar) = handle.try_state::<SidecarHandle>() {
                sidecar.current().shutdown();
            }
        }
    });
}
