use std::{
    env,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use tauri::{Manager, RunEvent};
use url::Url;

struct Backend(Mutex<Option<Child>>);

fn sidecar_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    {
        Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../dist/NO钱包自动化"))
    }
    #[cfg(not(debug_assertions))]
    {
        app.path()
            .resource_dir()
            .map_err(|error| error.to_string())
            .map(|path| path.join("nopay-python"))
    }
}

fn reserve_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|error| error.to_string())?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| error.to_string())
}

fn post_json(port: u16, path: &str, body: &str) -> Result<(), String> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).map_err(|error| error.to_string())?;
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|error| error.to_string())?;
    let mut response = String::new();
    stream.read_to_string(&mut response).map_err(|error| error.to_string())?;
    if response.starts_with("HTTP/1.0 2") || response.starts_with("HTTP/1.1 2") {
        Ok(())
    } else {
        Err(format!("dashboard request failed: {response}"))
    }
}

fn start_backend(app: &tauri::AppHandle) -> Result<(Child, u16), String> {
    let sidecar = sidecar_dir(app)?;
    let executable = sidecar.join("NO钱包自动化.exe");
    if !executable.is_file() {
        return Err(format!("找不到 Python sidecar：{}", executable.display()));
    }
    let config = sidecar.join("configs").join("qjd67.json");
    let port = reserve_port()?;
    let mut command = Command::new(executable);
    command
        .args(["dashboard", "--config"])
        .arg(config)
        .args(["--host", "127.0.0.1", "--port", &port.to_string()])
        .current_dir(&sidecar);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let child = command.spawn().map_err(|error| error.to_string())?;
    Ok((child, port))
}

fn wait_for_dashboard(port: u16) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err("本地控制台启动超时".into())
}

fn stop_backend(app: &tauri::AppHandle) {
    if let Ok(mut guard) = app.state::<Backend>().0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let app_handle = app.handle().clone();
            let (mut child, port) = start_backend(&app_handle).map_err(|error| Box::<dyn std::error::Error>::from(error))?;
            if let Err(error) = wait_for_dashboard(port) {
                let _ = child.kill();
                return Err(Box::<dyn std::error::Error>::from(error));
            }
            // 支付密码只能来自当前用户会话的环境变量；未设置时由控制台手动输入。
            if let Ok(password) = env::var("QJD67_PAY_PASSWORD") {
                let password = password.trim();
                if password.len() == 6 && password.bytes().all(|value| value.is_ascii_digit()) {
                    post_json(port, "/api/password", &format!("{{\"password\":\"{password}\"}}"))
                        .map_err(|error| Box::<dyn std::error::Error>::from(error))?;
                }
            }
            post_json(port, "/api/bot/start", "{}")
                .map_err(|error| Box::<dyn std::error::Error>::from(error))?;
            *app_handle.state::<Backend>().0.lock().map_err(|_| "backend state lock failed")? = Some(child);
            let url = Url::parse(&format!("http://127.0.0.1:{port}")).map_err(|error| error.to_string())?;
            app_handle
                .get_webview_window("main")
                .ok_or("主窗口不存在")?
                .navigate(url)
                .map_err(|error| error.to_string())?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Tauri application");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            stop_backend(app_handle);
        }
    });
}
