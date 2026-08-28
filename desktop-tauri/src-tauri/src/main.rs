// Release builds are a desktop GUI application.  Without this Windows opens a
// console window for the Tauri host process every time the app is launched.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    ffi::c_void,
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use tauri::{menu::{Menu, MenuItem}, tray::TrayIconBuilder, Manager, RunEvent, WindowEvent};
use url::Url;

struct BackendProcess {
    child: Child,
    port: u16,
}

struct Backend(Mutex<Option<BackendProcess>>);

#[cfg(windows)]
unsafe extern "system" {
    fn MessageBoxW(
        window: *mut c_void,
        text: *const u16,
        caption: *const u16,
        style: u32,
    ) -> i32;
}

#[cfg(windows)]
fn close_program_selected() -> bool {
    const MB_YESNO: u32 = 0x0000_0004;
    const MB_ICONQUESTION: u32 = 0x0000_0020;
    const IDYES: i32 = 6;
    let text: Vec<u16> = "是否停止脚本并退出程序？\r\n\r\n是：停止脚本、OCR 和浏览器后退出\r\n否：最小化到系统托盘并继续运行\0"
        .encode_utf16()
        .collect();
    let caption: Vec<u16> = "NO 钱包自动化\0".encode_utf16().collect();
    // This is a native Windows dialog, so it remains available even while the
    // dashboard page is loading or the webview JavaScript is unresponsive.
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            text.as_ptr(),
            caption.as_ptr(),
            MB_YESNO | MB_ICONQUESTION,
        ) == IDYES
    }
}

#[cfg(not(windows))]
fn close_program_selected() -> bool {
    false
}

fn sidecar_dir(_app: &tauri::AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    {
        Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../dist/NO钱包自动化"))
    }
    #[cfg(not(debug_assertions))]
    {
        _app.path()
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

fn user_data_root(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    match env::var_os("LOCALAPPDATA") {
        Some(path) => Ok(PathBuf::from(path).join("NO钱包自动化")),
        None => app.path().app_local_data_dir().map_err(|error| error.to_string()),
    }
}

fn dashboard_config_path(app: &tauri::AppHandle, sidecar: &std::path::Path) -> Result<PathBuf, String> {
    // Keep user-selected settings outside the installer resource directory.
    // NSIS replaces bundled resources on upgrade, while this path survives
    // reinstalls and is shared with the DPAPI-protected dashboard secrets.
    let data_root = user_data_root(app)?;
    let saved_config = data_root.join("config").join("qjd67.json");
    if saved_config.is_file() {
        return Ok(saved_config);
    }

    let bundled_config = sidecar.join("configs").join("qjd67.json");
    if !bundled_config.is_file() {
        return Err(format!("找不到初始配置：{}", bundled_config.display()));
    }
    let parent = saved_config
        .parent()
        .ok_or_else(|| "无法创建配置目录".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    fs::copy(&bundled_config, &saved_config).map_err(|error| error.to_string())?;
    Ok(saved_config)
}

fn persistent_browser_profile_path(
    app: &tauri::AppHandle,
    sidecar: &std::path::Path,
) -> Result<PathBuf, String> {
    let profile = user_data_root(app)?.join("browser-profile").join("qjd67");
    if profile.is_dir() {
        return Ok(profile);
    }

    let legacy_profile = sidecar.join(".browser-profile").join("qjd67");
    let parent = profile.parent().ok_or_else(|| "无法创建浏览器目录".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    if legacy_profile.is_dir() {
        // Retain the existing signed-in session when upgrading from versions
        // that kept the managed Chrome profile inside installer resources.
        #[cfg(windows)]
        {
            let result = Command::new("robocopy")
                .arg(&legacy_profile)
                .arg(&profile)
                .args(["/E", "/COPY:DAT", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"])
                .output()
                .map_err(|error| error.to_string())?;
            if result.status.code().map_or(true, |code| code > 7) {
                return Err(format!("迁移浏览器登录资料失败：{}", String::from_utf8_lossy(&result.stderr)));
            }
        }
        #[cfg(not(windows))]
        fs::create_dir_all(&profile).map_err(|error| error.to_string())?;
    } else {
        fs::create_dir_all(&profile).map_err(|error| error.to_string())?;
    }
    Ok(profile)
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
    let config = dashboard_config_path(app, &sidecar)?;
    let browser_profile = persistent_browser_profile_path(app, &sidecar)?;
    let port = reserve_port()?;
    let mut command = Command::new(executable);
    command
        .args(["dashboard", "--config"])
        .arg(config)
        .args(["--host", "127.0.0.1", "--port", &port.to_string()])
        .env("QJD67_USER_DATA_DIR", browser_profile)
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

#[cfg(windows)]
fn terminate_process_tree(process_id: u32) {
    use std::os::windows::process::CommandExt;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let _ = Command::new("taskkill")
        .args(["/PID", &process_id.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .status();
}

fn stop_backend(app: &tauri::AppHandle) {
    if let Ok(mut guard) = app.state::<Backend>().0.lock() {
        if let Some(backend) = guard.take() {
            // Dashboard stop waits for CanvasBot to close Playwright/Chrome.
            // Only the dashboard process itself is force-terminated afterward.
            let _ = post_json(backend.port, "/api/bot/stop", "{}");
            #[cfg(windows)]
            terminate_process_tree(backend.child.id());
            #[cfg(not(windows))]
            let child = &mut backend.child;
            #[cfg(not(windows))]
            {
            let _ = child.kill();
            let _ = child.wait();
            }
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let app_handle = app.handle().clone();
            let show = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出程序", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::with_id("main-tray")
                .icon(app.default_window_icon().ok_or("缺少应用图标")?.clone())
                .tooltip("NO 钱包自动化正在运行")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            if let Some(window) = app.get_webview_window("main") {
                let window_for_close = window.clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        if close_program_selected() {
                            window_for_close.app_handle().exit(0);
                        } else {
                            let _ = window_for_close.hide();
                        }
                    }
                });
            }
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
            *app_handle.state::<Backend>().0.lock().map_err(|_| "backend state lock failed")? = Some(BackendProcess { child, port });
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
