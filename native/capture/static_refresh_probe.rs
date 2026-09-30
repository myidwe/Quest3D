//! Test-only real WGC refresh experiment; never linked into the product wheel.
use super::*;
use std::sync::atomic::AtomicU64;
use std::time::Instant;
use windows::Foundation::TypedEventHandler;
use windows::Graphics::Capture::{Direct3D11CaptureFrame, Direct3D11CaptureFramePool};
use windows::Graphics::DirectX::DirectXPixelFormat;
use windows::Win32::Graphics::Direct3D11::{D3D11_CPU_ACCESS_READ, D3D11_MAP_READ,
    D3D11_MAPPED_SUBRESOURCE, D3D11_USAGE_STAGING};
use windows::Win32::System::Threading::{GetCurrentProcess, GetProcessHandleCount};
use windows::Win32::System::WinRT::Direct3D11::IDirect3DDxgiInterfaceAccess;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn QueryPerformanceCounter(value: *mut i64) -> i32;
    fn QueryPerformanceFrequency(value: *mut i64) -> i32;
}

fn qpc_ns() -> u64 {
    let (mut value, mut frequency) = (0, 0);
    unsafe {
        assert_ne!(QueryPerformanceCounter(&mut value), 0);
        assert_ne!(QueryPerformanceFrequency(&mut frequency), 0);
    }
    ((value as u128 * 1_000_000_000) / frequency as u128) as u64
}

fn pump() {
    let mut msg = MSG::default();
    unsafe {
        while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}

fn quiet(pool: &Direct3D11CaptureFramePool) -> (u128, u32) {
    let deadline = Instant::now() + Duration::from_secs(6);
    let mut since = Instant::now();
    let mut discarded = 0;
    loop {
        pump();
        while let Ok(frame) = pool.TryGetNextFrame() {
            frame.Close().unwrap();
            discarded += 1;
            since = Instant::now();
        }
        if since.elapsed() >= Duration::from_millis(1100) { return (since.elapsed().as_millis(), discarded); }
        assert!(Instant::now() < deadline, "Static no-new-frame baseline did not occur; no fixture paint allowed");
        thread::sleep(Duration::from_millis(2));
    }
}

fn next_frame(pool: &Direct3D11CaptureFramePool) -> Option<Direct3D11CaptureFrame> {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        pump();
        if let Ok(frame) = pool.TryGetNextFrame() { return Some(frame); }
        if Instant::now() >= deadline { return None; }
        thread::sleep(Duration::from_millis(1));
    }
}

fn inspect_texture(device: &ID3D11Device, context: &ID3D11DeviceContext,
    texture: &ID3D11Texture2D, width: u32, height: u32) -> (u64, [u16; 4]) {
    let mut desc = D3D11_TEXTURE2D_DESC::default();
    unsafe { texture.GetDesc(&mut desc); }
    assert_eq!(desc.Format.0, 10);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ.0 as u32;
    desc.MiscFlags = 0;
    let mut staging = None;
    unsafe { device.CreateTexture2D(&desc, None, Some(&mut staging)).unwrap(); }
    let staging = staging.unwrap();
    unsafe { context.CopyResource(&staging, texture); }
    let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
    unsafe { context.Map(&staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped)).unwrap(); }
    let mut hash = 0xcbf29ce484222325u64;
    let mut pixel = [0u16; 4];
    unsafe {
        for y in 0..height as usize {
            let row = (mapped.pData as *const u8).add(y * mapped.RowPitch as usize);
            for value in std::slice::from_raw_parts(row, width as usize * 8) {
                hash = (hash ^ *value as u64).wrapping_mul(0x100000001b3);
            }
        }
        let center = (mapped.pData as *const u8)
            .add(height as usize / 2 * mapped.RowPitch as usize + width as usize / 2 * 8);
        for (index, value) in pixel.iter_mut().enumerate() { *value = std::ptr::read_unaligned(center.add(index * 2) as *const u16); }
        context.Unmap(&staging, 0);
    }
    (hash, pixel)
}

fn wrap<'a>(raw: Direct3D11CaptureFrame, device: &'a ID3D11Device, context: &'a ID3D11DeviceContext) -> Frame<'a> {
    let surface = raw.Surface().unwrap();
    let access = surface.cast::<IDirect3DDxgiInterfaceAccess>().unwrap();
    let texture = unsafe { access.GetInterface::<ID3D11Texture2D>().unwrap() };
    let mut desc = D3D11_TEXTURE2D_DESC::default();
    unsafe { texture.GetDesc(&mut desc); }
    Frame::new(raw, device, surface, texture, context, desc, ColorFormat::Rgba16F, None)
}

#[test]
#[ignore = "owned static HWND only; use probe-static-refresh.py"]
fn actual_static_same_size_refresh() {
    let hwnd: isize = std::env::var("QUEST3D_PROBE_HWND").unwrap().parse().unwrap();
    let expected_pid: u32 = std::env::var("QUEST3D_PROBE_PID").unwrap().parse().unwrap();
    let identity = query_window_identity(hwnd).unwrap();
    assert_eq!(identity.0, expected_pid);
    let runtime = Arc::new(MtaLease(unsafe { CoIncrementMTAUsage().unwrap() }.0 as usize));
    unsafe { RoInitialize(RO_INIT_MULTITHREADED).unwrap(); }
    let _winrt = WinRTThread;
    let (device, context) = create_d3d_device_with_luid(None).unwrap();
    let slot = Arc::new(FrameSlot { data: Mutex::new(SlotData::default()), condvar: Condvar::new() });
    let mut handler = InnerHandler { runtime: runtime.clone(), shared_slot: slot.clone(), shared_texture: None,
        current_aligned_width: 0, current_aligned_height: 0, current_format: 0,
        thread_id: unsafe { GetCurrentThreadId() }, window_hwnd: Some(hwnd), window_identity: Some(identity.clone()) };
    let shutdown_error = Arc::new(Mutex::new(None));
    {
        let _queue = DispatcherThread { controller: unsafe { CreateDispatcherQueueController(DispatcherQueueOptions {
            dwSize: std::mem::size_of::<DispatcherQueueOptions>() as u32,
            threadType: DQTYPE_THREAD_CURRENT, apartmentType: DQTAT_COM_NONE }).unwrap() }, error: shutdown_error.clone() };
        let item_type: GraphicsCaptureItemType = Window::from_raw_hwnd(hwnd as _).try_into().unwrap();
        let item = match &item_type { GraphicsCaptureItemType::Window((item, _)) => item, _ => unreachable!() };
        let size = item.Size().unwrap();
        let direct = windows_capture::d3d11::create_direct3d_device(&device).unwrap();
        let pool = Direct3D11CaptureFramePool::Create(&direct, DirectXPixelFormat(10), 1, size).unwrap();
        let events = Arc::new(AtomicU64::new(0));
        let event_receipt = Arc::new(AtomicU64::new(0));
        let counts = events.clone();
        let receipt = event_receipt.clone();
        let token = pool.FrameArrived(&TypedEventHandler::new(move |_: windows::core::Ref<'_, Direct3D11CaptureFramePool>, _| {
            counts.fetch_add(1, Ordering::SeqCst);
            receipt.store(qpc_ns(), Ordering::SeqCst);
            Ok(())
        })).unwrap();
        let session = pool.CreateCaptureSession(item).unwrap();
        session.SetIsCursorCaptureEnabled(false).unwrap();
        session.StartCapture().unwrap();
        let first = next_frame(&pool).expect("Initial actual source frame missing");
        let mut first = wrap(first, &device, &context);
        handler.on_frame_arrived(&mut first, InternalCaptureControl::new(Arc::new(AtomicBool::new(false)))).unwrap();
        drop(first);
        let mut previous_stamp = slot.data.lock().frame.as_ref().unwrap().system_relative_time_ns;
        let mut refresh_id = 0;
        for iteration in 0..20 {
            let hold_lease = iteration % 4 == 0;
            for lease_phase in 0..(if hold_lease { 2 } else { 1 }) {
                let (quiet_ms, baseline_frames) = quiet(&pool);
                verify_window_identity(Some(hwnd), Some(&identity)).unwrap();
                assert_eq!(item.Size().unwrap(), size);
                let held = hold_lease && lease_phase == 0;
                let before = slot.data.lock().frame_id;
                let old = slot.data.lock().frame.as_ref().unwrap().clone();
                let old_pixel = inspect_texture(&device, &context, &old.texture, size.Width as u32, size.Height as u32);
                if held { assert!(slot.data.lock().lease.acquire(before)); }
                let events_before = events.load(Ordering::SeqCst);
                let requested_ns = qpc_ns();
                pool.Recreate(&direct, DirectXPixelFormat(10), 1, size).unwrap();
                let recreate_return_ns = qpc_ns();
                let raw = match next_frame(&pool) {
                    Some(frame) => frame,
                    None => {
                        let observed_ns = qpc_ns();
                        assert_eq!(slot.data.lock().frame_id, before);
                        assert_eq!(inspect_texture(&device, &context, &old.texture, size.Width as u32, size.Height as u32), old_pixel);
                        if held { assert!(slot.data.lock().lease.release(before)); }
                        let mut handles = 0;
                        unsafe { GetProcessHandleCount(GetCurrentProcess(), &mut handles).unwrap(); }
                        println!("Q3D_REFRESH {{\"refresh_id\":{refresh_id},\"quiet_ms\":{quiet_ms},\"baseline_discarded\":{baseline_frames},\"requested_ns\":{requested_ns},\"recreate_return_ns\":{recreate_return_ns},\"observed_ns\":{observed_ns},\"timed_out\":true,\"previous_raw_ns\":{previous_stamp},\"width\":{},\"height\":{},\"old_hash\":{},\"center_rgba16_bits\":{:?},\"lease_held\":{held},\"before_frame_id\":{before},\"after_frame_id\":{before},\"events_before\":{events_before},\"events\":{},\"handles\":{handles}}}",
                            size.Width, size.Height, old_pixel.0, old_pixel.1, events.load(Ordering::SeqCst));
                        refresh_id += 1;
                        let mut ack = String::new();
                        assert!(std::io::stdin().read_line(&mut ack).unwrap() > 0);
                        assert_eq!(ack.trim(), "observed");
                        break; // Next independent quiet+refresh trial, not a fake frame.
                    }
                };
                let received_ns = qpc_ns();
                let content = raw.ContentSize().unwrap();
                assert_eq!(content, size);
                let raw_stamp = raw.SystemRelativeTime().unwrap().Duration as u64 * 100;
                let mut frame = wrap(raw, &device, &context);
                let source_pixels = inspect_texture(&device, &context, frame.as_raw_texture(), size.Width as u32, size.Height as u32);
                assert_eq!(source_pixels.1, old_pixel.1, "Static source center pixel changed");
                handler.on_frame_arrived(&mut frame, InternalCaptureControl::new(Arc::new(AtomicBool::new(false)))).unwrap();
                drop(frame);
                let after = slot.data.lock().frame_id;
                assert!(events.load(Ordering::SeqCst) > events_before, "No actual FrameArrived event");
                if held {
                    assert_eq!(before, after, "Writer published while consumer held its lease");
                    assert_eq!(inspect_texture(&device, &context, &old.texture, size.Width as u32, size.Height as u32), old_pixel);
                    assert!(slot.data.lock().lease.release(before));
                } else { assert_eq!(after, before + 1); }
                let mut handles = 0;
                unsafe { GetProcessHandleCount(GetCurrentProcess(), &mut handles).unwrap(); }
                println!("Q3D_REFRESH {{\"refresh_id\":{refresh_id},\"quiet_ms\":{quiet_ms},\"baseline_discarded\":{baseline_frames},\"requested_ns\":{requested_ns},\"recreate_return_ns\":{recreate_return_ns},\"timed_out\":false,\"event_receipt_ns\":{},\"received_ns\":{received_ns},\"source_raw_ns\":{raw_stamp},\"previous_raw_ns\":{previous_stamp},\"width\":{},\"height\":{},\"source_hash\":{},\"old_hash\":{},\"center_rgba16_bits\":{:?},\"lease_held\":{held},\"before_frame_id\":{before},\"after_frame_id\":{after},\"events_before\":{events_before},\"events\":{},\"handles\":{handles}}}",
                    event_receipt.load(Ordering::SeqCst), size.Width, size.Height, source_pixels.0, old_pixel.0, source_pixels.1, events.load(Ordering::SeqCst));
                previous_stamp = raw_stamp;
                refresh_id += 1;
                let mut ack = String::new();
                assert!(std::io::stdin().read_line(&mut ack).unwrap() > 0);
                assert_eq!(ack.trim(), "observed");
            }
        }
        session.Close().unwrap();
        pool.RemoveFrameArrived(token).unwrap();
        pool.Close().unwrap();
    }
    assert!(shutdown_error.lock().is_none());
}
