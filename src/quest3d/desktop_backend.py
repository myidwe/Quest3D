"""Local desktop lifecycle controller for the validated monitor-only product.

No network API, automatic downloads, OS input or forced process termination.
The UI reads a cached snapshot; all capture/host work is serialized off its thread.
"""
from __future__ import annotations

import copy
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
import uuid

import psutil

from .assets import sha256_file, verified_model
from . import audio_output
from .paths import ROOT
from .model_choice import DEFAULT_DEPTH_MODEL, DAD_DEPTH_MODEL, DEPTH_MODEL_IDS, DEPTH_MODEL_LABELS, validate_depth_model
from .desktop_setup import distribution_layout
from .session_control import atomic_json, read_json, send_control, validate_request

HOST_SHA = '77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63'
RUNTIME = 'artifacts/host/runtime-20260909-233013-03efb525'
HDR_PACKAGE = 'artifacts/capture/hdr-experimental/package-0d8bea69406e'
OUTPUT_PROFILES = {'quest2': (1920, 1080), 'quest3': (2048, 1152)}
AI_QUALITY_PROFILES = {'standard': 280, 'quality': 322}
AI_QUALITY_LABELS = {'standard': 'Standard', 'quality': 'Quality · Preview'}
DEFAULTS = {'version': 1, 'monitor_device': '', 'mode': '3d', 'depth_percent': 1.3125,
            'profile': 'comfort', 'output_profile': 'quest2', 'depth_model': DEFAULT_DEPTH_MODEL,
            'ai_quality': 'standard', 'audio_output': 'pc'}


def output_eye_text(profile):
    width, height = OUTPUT_PROFILES[profile]
    return f'눈별 {width} × {height} · 16:9'


def validate_ai_quality(value):
    if not isinstance(value, str) or value not in AI_QUALITY_PROFILES:
        raise ValueError('AI Quality는 Standard 또는 Quality 선택')
    return value


def ai_quality_options():
    return [{'id': key, 'label': AI_QUALITY_LABELS[key], 'ai_size': size}
            for key, size in AI_QUALITY_PROFILES.items()]


def ai_quality_status(preference, status=None):
    """Keep next-start preference separate from unknown/legacy active metadata."""
    label = AI_QUALITY_LABELS[preference]
    values = dict(ai_quality=preference, ai_qualities=ai_quality_options(),
                  active_ai_quality=None, ai_size=None, ai_input_shape=None,
                  ai_input_text='다음 시작 · ' + ('세부 분석' if preference == 'quality' else '빠른 처리'))
    if status is not None:
        size = status.get('ai_size')
        active = next((key for key, value in AI_QUALITY_PROFILES.items() if value == size), None)
        values.update(active_ai_quality=active, ai_size=size)
        shape = status.get('ai_input_shape')
        if shape:
            values['ai_input_shape'] = list(shape)
            values['ai_input_text'] = f'실제 AI 입력 {shape[1]} × {shape[0]}'
            if size is not None:
                values['ai_input_text'] += f' · 요청 {size} px'
        elif size is not None:
            values['ai_input_text'] = f'AI 짧은 변 {size} px · 실제 입력 확인 전'
        else:
            values['ai_input_text'] = f'실행 중 AI 설정 확인 전 · 다음 시작 {label}'
        if active is not None:
            values['ai_quality'] = active
    return values


def validated_preferences(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('저장된 PC 설정 형식을 확인할 수 없습니다. 진단 폴더에서 설정을 확인해 주세요.')
    result = dict(DEFAULTS)
    result.update({k: value[k] for k in DEFAULTS if k in value})
    if result['mode'] not in ('2d', '3d') or result['profile'] not in ('comfort', 'linear'):
        raise ValueError('저장된 보기 설정이 올바르지 않습니다.')
    if not isinstance(result['output_profile'], str) or result['output_profile'] not in OUTPUT_PROFILES:
        raise ValueError('저장된 Headset 설정이 올바르지 않습니다.')
    validate_depth_model(result['depth_model'])
    validate_ai_quality(result['ai_quality'])
    audio_output.validate_output(result['audio_output'])
    depth = result['depth_percent']
    if isinstance(depth, bool) or not isinstance(depth, (int, float)) or not math.isfinite(depth) or not 0 <= depth <= 4:
        raise ValueError('Depth는 0~4% 범위의 숫자여야 합니다.')
    device = result['monitor_device']
    if not isinstance(device, str) or len(device) > 128:
        raise ValueError('저장된 모니터 선택이 올바르지 않습니다.')
    return result


def bounded_tail(path, limit=32768):
    try:
        with Path(path).open('rb') as file:
            file.seek(max(0, file.seek(0, 2)-limit))
            return file.read(limit).decode('utf-8-sig', errors='replace')
    except OSError:
        return ''


def identity(pid):
    """A retained PID is useful only together with executable and birth time."""
    process = psutil.Process(int(pid))
    if os.name == 'nt':
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        kernel.OpenProcess.restype = W.HANDLE
        kernel.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
        kernel.GetProcessTimes.restype = W.BOOL
        kernel.CloseHandle.argtypes = [W.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            born, ended, cpu, user = (W.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, C.byref(born), C.byref(ended), C.byref(cpu), C.byref(user)):
                raise C.WinError(C.get_last_error())
            birth = str((born.dwHighDateTime << 32) | born.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    else:
        birth = str(process.create_time())
    return {'pid': process.pid, 'exe': str(Path(process.exe()).resolve()), 'birth': birth}


def same_process(record, *, strict=False):
    try:
        return bool(record) and identity(record['pid']) == record
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        if strict:
            raise
        return False


def hidden_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class DesktopController:
    def __init__(self, root=ROOT, *, start_worker=True):
        self.root = Path(root).resolve()
        layout = distribution_layout(self.root, host_sha=HOST_SHA, runtime=RUNTIME, capture=HDR_PACKAGE)
        self.runtime = layout['runtime']
        self.capture_package = layout['capture']
        self.distributed = layout['distributed']
        self.python = self.root / '.venv/Scripts/python.exe'
        self.pwsh = self.root / '.tools/desktop/powershell/pwsh.exe'
        self.settings_path = self.root / 'config/desktop.json'
        self.logs = self.root / 'artifacts/desktop'
        self.logs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._commands = queue.Queue()
        self._quit = threading.Event()
        self._thread = None
        self._session = None
        self._producer = None
        self._host = None
        self._status = None
        self._status_error = None
        self._invalid_status_observation = None
        self._pending_path = self.logs/'owned-session.json'
        self._pending = read_json(self._pending_path) if self._pending_path.exists() else None
        self._helper_path = self.logs/'pending-helper.json'
        self._helper = read_json(self._helper_path) if self._helper_path.exists() else None
        self._verified_host = None
        self._connection_owner = None
        self._connection_value = None
        self._last_discovery = 0.0
        self._last_source_scan = 0.0
        self._last_counter = None
        self._last_log_dir = self.logs
        self._prefs_error = None
        self._health_error = None
        self._model_availability_cache = {}
        self._audio_options_cache = None
        self._audio_observation_owner = None
        self._audio_capture_state = 'waiting'
        try:
            self.preferences = validated_preferences(read_json(self.settings_path)) if self.settings_path.exists() else dict(DEFAULTS)
        except (ValueError, OSError) as exc:
            self.preferences = dict(DEFAULTS)
            self._prefs_error = str(exc)
        self._state = dict(phase='checking', busy=False, message='현재 PC 상태를 확인하고 있습니다.', error=None,
            running=False, ready=False, host_running=False, connected=None, connection_text='연결 확인 중',
            mode=self.preferences['mode'], depth_percent=self.preferences['depth_percent'],
            profile=self.preferences['profile'], monitors=[], selected_monitor=self.preferences['monitor_device'],
            depth_model=self.preferences['depth_model'], depth_models=self._depth_model_options(),
            depth_model_switching=False,
            output_profile=self.preferences['output_profile'],
            output_profiles=[{'id': key, 'label': 'Quest 2' if key == 'quest2' else 'Quest 3'}
                             for key in OUTPUT_PROFILES],
            eye_text=output_eye_text(self.preferences['output_profile']),
            quality_text='로컬 AI · 윤곽 안정화',
            pc_address='', metrics_text='', last_diagnostics=None, pairing_available=False, pairing_message='')
        self._state.update(ai_quality_status(self.preferences['ai_quality']))
        self._state.update(audio_output=self.preferences['audio_output'], audio_outputs=[],
                           active_audio_output=None, audio_status='출력 장치 확인 중', audio_error='')
        if start_worker:
            self._thread = threading.Thread(target=self._loop, name='Quest3D Desktop', daemon=True)
            self._thread.start()

    def get_snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def command(self, action, **kwargs):
        if action not in ('start', 'stop', 'refresh', 'control', 'select_monitor', 'select_output_profile', 'select_depth_model', 'select_ai_quality', 'select_audio_output', 'export_diagnostics',
                          'open_logs', 'open_guide', 'open_diagnostics', 'pair'):
            return False
        with self._lock:
            if self._state['busy'] or self._quit.is_set():
                return False
            self._state['busy'] = True
            self._commands.put((action, kwargs))
        return True

    def close(self):
        """Close UI polling only. Caller must complete an explicit stop first."""
        self._quit.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _event(self, kind, **data):
        path = self.logs/'events.jsonl'
        if path.exists() and path.stat().st_size > 2*1024*1024:
            os.replace(path, path.with_suffix('.previous.jsonl'))
        with path.open('a', encoding='utf-8') as file:
            file.write(json.dumps({'time': datetime.now().astimezone().isoformat(), 'event': kind, **data}, ensure_ascii=False)+'\n')

    def _save_preferences(self):
        if self._prefs_error:
            return  # Preserve malformed user data for recovery; never overwrite it silently.
        existing = read_json(self.settings_path) if self.settings_path.exists() else None
        if existing != self.preferences:
            atomic_json(self.settings_path, self.preferences)

    def _depth_model_options(self):
        """Cache file verification for the UI; startup/selection checks again."""
        try:
            specs = read_json(self.root/'config/models.json')
        except (ValueError, OSError):
            specs = {}
        rows = []
        for key in DEPTH_MODEL_IDS:
            available = False
            try:
                spec = specs[key]
                path = (self.root/'models'/spec['filename']).resolve()
                if not path.is_relative_to((self.root/'models').resolve()):
                    raise ValueError('Model path outside models directory')
                stat = path.stat()
                signature = (str(path), stat.st_size, stat.st_mtime_ns, spec['sha256'], spec['bytes'])
                cached = self._model_availability_cache.get(key)
                if cached is None or cached[0] != signature:
                    available = stat.st_size == spec['bytes'] and sha256_file(path) == spec['sha256']
                    self._model_availability_cache[key] = (signature, available)
                else:
                    available = cached[1]
            except (KeyError, TypeError, ValueError, OSError):
                self._model_availability_cache.pop(key, None)
            rows.append({'id': key, 'label': DEPTH_MODEL_LABELS[key], 'available': available})
        return rows

    def _loop(self):
        while not self._quit.is_set():
            try:
                try:
                    action, kwargs = self._commands.get(timeout=.5)
                except queue.Empty:
                    action, kwargs = None, {}
                if action:
                    self._set(error=None)
                    self._event('command', action=action, arguments={} if action == 'pair' else kwargs)
                    try:
                        getattr(self, '_'+action)(**kwargs)
                    except Exception as exc:
                        self._event('command_failed', action=action, error=repr(exc))
                        if action == 'pair':
                            # A failed PIN must not label an otherwise healthy
                            # video pipeline as broken or request a restart.
                            self._set(pairing_message=str(exc))
                        else:
                            self._set(phase='error', error=str(exc), message='작업을 마치지 못했습니다. 아래 안내를 확인해 주세요.')
                    finally:
                        self._set(busy=False)
                self._inspect()
            except Exception as exc:
                self._set(phase='error', error=str(exc), message='PC 상태를 확인하지 못했습니다. 상태 새로고침을 눌러 주세요.', busy=False)
                self._quit.wait(1)

    def _scan_sources(self):
        from .capture import list_monitors
        monitors = list_monitors()[1:]
        choices = [{'device_name': m.device_name, 'index': m.index,
                    'width': m.bounds.width, 'height': m.bounds.height,
                    'label': f"모니터 {m.index} · {m.bounds.width} × {m.bounds.height}" + (' · 주 화면' if m.is_primary else '')}
                   for m in monitors]
        self._set(monitors=choices)
        if not self.preferences['monitor_device'] and choices:
            chosen = next((m for m in monitors if m.is_primary), monitors[0])
            self.preferences['monitor_device'] = chosen.device_name
        self._set(selected_monitor=self.preferences['monitor_device'])
        self._last_source_scan = time.monotonic()
        addresses = []
        for name, entries in psutil.net_if_addrs().items():
            for row in entries:
                if row.family == socket.AF_INET:
                    ip = ipaddress.ip_address(row.address)
                    if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                        addresses.append((not row.address.startswith('192.168.'), row.address))
        self._set(pc_address=sorted(set(addresses))[0][1] if addresses else '사용 가능한 사설망 주소 없음')

    def _read_session_status(self, directory, expected_session_id=None):
        """A damaged observation is not proof that its producer has stopped.

        Keep the directory and all ownership records for process discovery.
        Never repair the producer's file or relax control/ownership JSON reads.
        """
        path = directory/'status.json'
        self._status_error = None
        try:
            status = read_json(path)
            if (not isinstance(status, dict) or not isinstance(status.get('session_id'), str)
                    or not status['session_id'] or type(status.get('running')) is not bool):
                raise ValueError('Invalid session status object')
            if expected_session_id is not None and status['session_id'] != expected_session_id:
                raise RuntimeError('활성 세션 정보가 일치하지 않습니다. 진단을 저장해 주세요.')
            if status['running']:
                for key in ('eye_width', 'eye_height', 'updated_monotonic_ns',
                            'published_frames', 'stream_epoch'):
                    if type(status.get(key)) is not int or status[key] < 0:
                        raise ValueError('Invalid session status field: '+key)
                if not status['eye_width'] or not status['eye_height']:
                    raise ValueError('Invalid session dimensions')
                if 'ai_size' in status and (type(status['ai_size']) is not int
                        or not 140 <= status['ai_size'] <= 1036 or status['ai_size'] % 14):
                    raise ValueError('Invalid AI input short edge')
                shape = status.get('ai_input_shape')
                if shape is not None and (not isinstance(shape, list) or len(shape) != 2
                        or any(type(value) is not int or not 0 < value <= 16384 for value in shape)):
                    raise ValueError('Invalid actual AI input shape')
                disparity = status.get('disparity')
                if (type(disparity) not in (int, float) or not math.isfinite(disparity)
                        or not 0 <= disparity <= status['eye_width'] * .04
                        or status.get('requested_mode') not in ('2d', '3d')
                        or type(status.get('ai_completed_published', 0)) is not int):
                    raise ValueError('Invalid session view state')
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as exc:
            self._status_error = '이전 실행의 상태 기록을 읽을 수 없습니다. 실행 중인 프로세스를 확인합니다.'
            info = path.stat()
            observation = (str(path), info.st_mtime_ns, info.st_size)
            if observation != self._invalid_status_observation:
                self._event('invalid_session_status', path=str(path), error=str(exc),
                            bytes=info.st_size, preserved=True)
                self._invalid_status_observation = observation
            return None
        return status

    def _active_session(self):
        marker = self.root/'artifacts/active-session.json'
        if not marker.exists():
            return None, None
        value = read_json(marker)
        path = Path(value['directory']).resolve()
        if not path.is_relative_to(self.root/'artifacts'):
            raise RuntimeError('활성 세션 경로가 프로젝트 밖입니다. 기존 실행을 변경하지 않았습니다.')
        return path, self._read_session_status(path, value['session_id'])

    def _find_producer(self, directory):
        found = []
        for process in psutil.process_iter(['pid', 'name']):
            if (process.info['name'] or '').casefold() not in ('python.exe', 'pythonw.exe'):
                continue
            try:
                argv = process.cmdline() or []
                if 'serve' not in argv or '--output' not in argv:
                    continue
                cwd = Path(process.cwd()).resolve()
                output = (cwd/argv[argv.index('--output')+1]).resolve()
                entry = str(self.root/'.venv/Scripts/quest3d.exe').casefold()
                known_entry = any(str(x).replace('/', '\\').casefold() == entry.replace('/', '\\') for x in argv)
                known_module = '-m' in argv and argv[argv.index('-m')+1] == 'quest3d.cli'
                if cwd == self.root and output == directory and (known_entry or known_module) and Path(process.exe()).name.casefold() in ('python.exe','pythonw.exe'):
                    if '--monitor' not in argv or set(argv) & {'--window','--rect','--inline-rect','--file'}:
                        raise RuntimeError('이 PC 앱은 모니터 송출만 관리합니다. 다른 실행은 보존했습니다.')
                    found.append((process, argv))
            except psutil.AccessDenied as exc:
                raise RuntimeError('영상 처리 프로세스의 실행 정보를 확인할 권한이 없습니다. 중복 시작을 중단했습니다.') from exc
            except (psutil.NoSuchProcess, ValueError, IndexError, OSError):
                continue
        parents = {p.ppid() for p, _ in found}
        leaves = [(p, a) for p, a in found if p.pid not in parents]
        if len(leaves) > 1:
            raise RuntimeError('동일 세션의 처리 프로세스가 여러 개입니다. 중복 실행을 보존하고 시작을 멈췄습니다.')
        if not leaves:
            return None, None
        process, argv = leaves[0]
        return identity(process.pid), argv

    def _host_identity(self):
        path = self.root/'artifacts/host/dev/process.json'
        if not path.exists():
            return None
        data = read_json(path)
        try:
            record = identity(data['process_id'])
        except psutil.NoSuchProcess:
            return None
        if (Path(record['exe']) != (self.runtime/'sunshine.exe').resolve()
                or record['birth'] != str(data['owner_creation_filetime'])):
            raise RuntimeError('호스트 PID 또는 생성 시각이 달라졌습니다. 다른 프로세스는 종료하지 않습니다.')
        return record

    def _inspect(self):
        # Observations can recover while another UI/controller is starting the
        # pipeline. Clear only our own prior health error, never a command error.
        if self._health_error and self.get_snapshot().get('error') == self._health_error:
            self._set(error=None)
        self._health_error = None
        now = time.monotonic()
        if now-self._last_source_scan > 10:
            self._scan_sources()
        directory, status = self._active_session()
        # A producer can fail before it publishes the active-session marker.
        # Keep its explicit launch record across polling and UI restarts.
        if self._pending:
            pending_dir = Path(self._pending['directory']).resolve()
            if not pending_dir.is_relative_to(self.root/'artifacts'):
                raise RuntimeError('대기 중 실행의 경로가 올바르지 않습니다.')
            pending_producer, _ = self._find_producer(pending_dir)
            if pending_producer or same_process(self._pending.get('launcher_identity'), strict=True):
                if directory and directory != pending_dir and self._find_producer(directory)[0]:
                    raise RuntimeError('서로 다른 영상 처리 세션이 실행 중입니다. 기존 실행 기록을 보존했습니다.')
                directory = pending_dir
                status = self._read_session_status(directory)
        if directory != self._session or not same_process(self._producer) or now-self._last_discovery > 5:
            self._session = directory
            self._producer, argv = self._find_producer(directory) if directory else (None, None)
            self._last_discovery = now
            if argv and '--monitor' in argv:
                index = int(argv[argv.index('--monitor')+1])
                choice = next((m for m in self.get_snapshot()['monitors'] if m['index'] == index), None)
                if choice:
                    self.preferences['monitor_device'] = choice['device_name']
        self._host = self._host_identity()
        if status is not None:
            # Process enumeration can take seconds on a busy Windows machine.
            # Evaluate a fresh snapshot after it, not a pre-enumeration frame.
            status = self._read_session_status(directory, status['session_id'])
        running = bool(self._producer or (self._pending and same_process(self._pending.get('launcher_identity'), strict=True)))
        ready = bool(running and status and status.get('running'))
        self._status = status
        host_ok = bool(self._host)
        # A live host is only usable with its exact validated producer binding.
        if host_ok:
            launch = read_json(self.root/'artifacts/host/dev/launch.json')
            bound = launch.get('source_session', {})
            if ready and (bound.get('session_id') != status['session_id'] or str(bound.get('stream_epoch')) != str(status['stream_epoch'])):
                self._health_error = 'PC 처리와 전송 서버의 세션이 다릅니다. 중지 후 다시 시작해 주세요.'
                self._set(phase='conflict', error=self._health_error)
        changes = dict(running=running, ready=ready, host_running=host_ok, pairing_available=host_ok,
                       depth_model=self.preferences['depth_model'], depth_models=self._depth_model_options(),
                       depth_model_switching=False,
                       selected_monitor=self.preferences['monitor_device'],
                       output_profile=self.preferences['output_profile'],
                       eye_text=output_eye_text(self.preferences['output_profile']))
        changes.update(ai_quality_status(self.preferences['ai_quality']))
        if ready:
            if status.get('media') or status.get('view_layout') != 'enlarged' or status.get('bridge_protocol') != 2:
                raise RuntimeError('현재 실행은 데스크톱 앱이 관리하는 모니터 보기 경로가 아닙니다. 기존 실행을 보존했습니다.')
            percent = float(status['disparity'])/int(status['eye_width'])*100
            if 'disparity_profile' not in status:
                raise RuntimeError('실행 중인 이전 버전은 PC 앱의 화질 제어를 지원하지 않습니다. 기존 실행을 먼저 종료해 주세요.')
            self.preferences.update(mode=status['requested_mode'], depth_percent=percent, profile=status['disparity_profile'])
            quality_state = ai_quality_status(self.preferences['ai_quality'], status)
            changes.update(quality_state)
            if quality_state['active_ai_quality'] is not None:
                self.preferences['ai_quality'] = quality_state['active_ai_quality']
            active_model = validate_depth_model(status.get('depth_model', DEFAULT_DEPTH_MODEL))
            changing_model = bool(status.get('depth_model_switching'))
            if not changing_model and not status.get('ai_error') and not status.get('error'):
                self.preferences['depth_model'] = active_model
                changes['depth_model'] = active_model
            changes['depth_model_switching'] = changing_model
            prepared = status.get('available_depth_models', [DEFAULT_DEPTH_MODEL])
            changes['depth_models'] = [{**row, 'available': row['available'] and row['id'] in prepared}
                                       for row in changes['depth_models']]
            # Reopening the UI adopts the actual running profile, including a
            # session started by another UI. Never infer it from the headset IP.
            geometry = (status['eye_width'], status['eye_height'])
            for profile_id, dimensions in OUTPUT_PROFILES.items():
                if dimensions == geometry:
                    self.preferences['output_profile'] = profile_id
                    changes['output_profile'] = profile_id
                    break
            changes.update(mode=status['requested_mode'], depth_percent=percent, profile=status['disparity_profile'],
                           eye_text=f"눈별 {status['eye_width']} × {status['eye_height']} · 16:9")
            age = (time.perf_counter_ns()-status['updated_monotonic_ns'])/1e9
            trouble = status.get('error') or status.get('ai_error') or status.get('capture_unavailable')
            if not 0 <= age <= 4:
                trouble = 'PC 처리 상태 갱신이 멈췄습니다. 중지 후 다시 시작해 주세요.'
            if trouble:
                changes.update(phase='error', ready=False, error=str(trouble), message='송출 상태를 확인해 주세요.')
            elif not self.get_snapshot()['error'] and not self.get_snapshot()['busy']:
                changes.update(phase='running', message='PC 영상 준비 완료. Quest 앱에서 이 PC에 연결하세요.' if host_ok else '영상은 준비됐습니다. 시작을 누르면 전송 서버를 연결합니다.')
            counter = (now, int(status['published_frames']), int(status.get('ai_completed_published', 0)))
            if self._last_counter and counter[0]-self._last_counter[0] >= 2:
                old = self._last_counter
                dt = counter[0]-old[0]
                if counter[1] >= old[1]:
                    changes['metrics_text'] = f"게시 {(counter[1]-old[1])/dt:.1f}회/s · 새 AI 결과 {(counter[2]-old[2])/dt:.1f}회/s (정지 화면은 반복 표시)"
                self._last_counter = counter
            elif not self._last_counter:
                self._last_counter = counter
        elif running:
            trouble = ('영상 처리 프로세스가 실행 중이지만 상태 기록이 손상되었습니다. 중복 시작을 중단했습니다. 진단을 저장해 주세요.'
                       if self._status_error else '영상 처리 프로세스가 있지만 준비된 영상이 없습니다. 로그를 확인하고 중지해 주세요.')
            changes.update(phase='error', ready=False, error=trouble)
        elif not self.get_snapshot()['busy'] and not self.get_snapshot()['error']:
            changes.update(phase='idle', message='시작을 누르면 PC 화면을 Quest로 보낼 준비를 합니다.', metrics_text='')
            if host_ok:
                changes.update(phase='error', error='영상 처리 없이 전송 서버만 실행 중입니다. 중지 후 다시 시작해 주세요.')
        if self._prefs_error:
            changes.update(phase='error', error=self._prefs_error)
        connected, text = self._connection(host_ok)
        changes.update(connected=connected, connection_text=text)
        self._inspect_audio(changes, host_ok, connected)
        if changes.get('error'):
            self._health_error = changes['error']
        changes['pairing_available'] = bool(host_ok and ready and not changes.get('error')
                                            and not self.get_snapshot().get('error'))
        self._set(**changes)
        self._save_preferences()

    def _inspect_audio(self, changes, host_ok, connected):
        now = time.monotonic()
        if self._audio_options_cache is None or now - self._audio_options_cache[0] > 10:
            self._audio_options_cache = (now, audio_output.options(self.root))
        active = None
        record = None
        if host_ok:
            record = read_json(self.root/'artifacts/host/dev/process.json')
            if (record.get('process_id') == self._host['pid']
                    and record.get('owner_creation_filetime') == self._host['birth']):
                candidate = record.get('audio_output', 'pc')
                if isinstance(candidate, str) and candidate in audio_output.OUTPUTS:
                    active = candidate
        label = audio_output.OUTPUTS.get(active, '')
        if host_ok:
            status = ('현재 설정 · ' + label if active is not None else '실행 중 오디오 설정 확인 필요')
            if active in ('quest', 'both'):
                status += ' · Quest 연결 중' if connected else ' · Quest 연결 대기'
        else:
            status = '다음 시작 · ' + audio_output.OUTPUTS[self.preferences['audio_output']]
        error = ''
        owner = (self._host['pid'], self._host['birth']) if host_ok else None
        if owner != self._audio_observation_owner:
            self._audio_observation_owner = owner
            self._audio_capture_state = 'waiting'
        if active in ('quest', 'both') and record:
            started = record.get('started_at', '').replace('T', ' ')[:23]
            for line in bounded_tail(self.root/'artifacts/host/dev/sunshine.log').splitlines():
                if not started or not line.startswith('[') or line[1:24] < started:
                    continue
                if 'CLIENT CONNECTED' in line:
                    self._audio_capture_state = 'starting'
                elif 'CLIENT DISCONNECTED' in line:
                    self._audio_capture_state = 'waiting'
                elif 'Opus initialized:' in line:
                    self._audio_capture_state = 'ready'
                elif ('Error:' in line and any(message in line for message in (
                        'audio capture', 'Audio route', 'audio sink', 'audio policy',
                        'Device Enumerator', 'audio watchdog', 'capture audio', 'microphone'))):
                    self._audio_capture_state = 'error'
            if self._audio_capture_state == 'error':
                error = '오디오 전송 실패 · PC 중지 후 출력 장치 확인'
            elif self._audio_capture_state == 'ready' and connected:
                status += ' · 오디오 준비됨'
        changes.update(audio_output=self.preferences['audio_output'], active_audio_output=active,
                       audio_outputs=self._audio_options_cache[1], audio_status=status, audio_error=error)

    def _recover_audio(self):
        path = self.root/'artifacts/host/dev/process.json'
        if not path.exists():
            return
        record = read_json(path)
        if not record.get('audio_directory'):
            return
        if Path(record['runtime']).resolve() != self.runtime.resolve():
            raise RuntimeError('오디오 복구 대상 전송 서버 불일치')
        result = audio_output.recover_stopped_host(self.root, self.runtime, record)
        self._event('audio_restore', **result)

    def _connection(self, host_ok):
        if not host_ok:
            self._connection_owner = self._connection_value = None
            return False, '전송 서버 중지됨'
        owner = read_json(self.root/'artifacts/host/dev/process.json')
        owner_key = (owner['process_id'], str(owner['owner_creation_filetime']))
        if owner_key != self._connection_owner:
            self._connection_owner, self._connection_value = owner_key, None
        started = owner.get('started_at', '').replace('T',' ')[:23]
        log = bounded_tail(self.root/'artifacts/host/dev/sunshine.log')
        for line in log.splitlines():
            # Appending logs from a previous host must never label a new host
            # connected. Cache current-lifetime markers as the tail rolls on.
            if not started or not line.startswith('[') or line[1:24] < started:
                continue
            if 'CLIENT CONNECTED' in line:
                self._connection_value = True
            elif 'CLIENT DISCONNECTED' in line:
                self._connection_value = False
        if self._connection_value is True:
            return True, 'Quest 연결 감지 · 실제 보이는 화면은 헤드셋에서 확인'
        if self._connection_value is False:
            return False, 'Quest 연결 대기 · 헤드셋에서 PC에 연결해 주세요'
        # Connection marker may leave the bounded log tail during a long session.
        return None, '전송 서버 실행 중 · Quest 앱에서 연결 상태 확인'

    def _environment(self):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(self.capture_package)
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PATH'] = os.pathsep.join([str(self.root/'.tools/desktop'), str(self.pwsh.parent),
                                      str(self.python.parent), env.get('PATH', '')])
        env['UV_OFFLINE'] = '1'
        env['UV_NO_SYNC'] = '1'
        return env

    def _run(self, argv, label, *, timeout=90):
        path = self._last_log_dir/(label+'.log')
        with path.open('wb') as output:
            child = subprocess.Popen([str(v) for v in argv], cwd=self.root, env=self._environment(),
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     creationflags=hidden_flags())
            self._helper = identity(child.pid)
            atomic_json(self._helper_path, self._helper)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # A lifecycle helper may own a host; do not kill it behind its back.
                self._event('helper_timeout', pid=child.pid, arguments=[str(v) for v in argv], log=str(path))
                raise RuntimeError(f'시작/종료 도구의 응답이 늦습니다. 중복 실행하지 않고 기록을 남겼습니다. {path.name}')
        self._helper = None
        self._helper_path.unlink(missing_ok=True)
        if code:
            raise RuntimeError(f'{label} 실패. 로그 폴더의 {path.name}을 확인해 주세요.\n'+bounded_tail(path, 1800))
        return path

    def _check_pending_helper(self):
        if self._helper and same_process(self._helper, strict=True):
            raise RuntimeError('이전 시작/종료 작업이 아직 실행 중입니다. 로그를 확인한 뒤 상태를 새로고침해 주세요.')
        self._helper = None
        self._helper_path.unlink(missing_ok=True)

    def _verify_host_ready(self, timeout=12):
        """Validate actual host ownership/listeners, not a launch receipt alone."""
        launch = read_json(self.root/'artifacts/host/dev/launch.json')
        if Path(launch['runtime']).resolve() != self.runtime or launch['host_sha256'].lower() != HOST_SHA:
            raise RuntimeError('전송 서버의 실행 경로/버전 기록이 다릅니다.')
        if self._verified_host != self._host:
            if sha256_file(self.runtime/'sunshine.exe') != HOST_SHA:
                raise RuntimeError('전송 서버의 실제 파일이 검증된 버전과 다릅니다.')
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if not same_process(self._host):
                raise RuntimeError('전송 서버가 연결 준비 중 종료됐습니다.')
            sockets = psutil.Process(self._host['pid']).net_connections(kind='tcp')
            listening = {s.laddr.port for s in sockets if s.status == psutil.CONN_LISTEN}
            if {47984,47989,48010}.issubset(listening):
                self._verified_host = dict(self._host)
                return
            time.sleep(.2)
        raise RuntimeError('전송 서버의 실제 연결 포트가 준비되지 않았습니다. 로그를 확인해 주세요.')

    def _preflight(self):
        if self._prefs_error:
            raise RuntimeError(self._prefs_error)
        for file in (self.python, self.pwsh, self.root/'.tools/desktop/uv.exe',
                     self.capture_package/'wc_cuda/__init__.py', self.runtime/'sunshine.exe'):
            if not file.is_file():
                raise RuntimeError(f'실행에 필요한 파일이 없습니다: {file.name}. 사용 안내의 복구 절차를 확인해 주세요.')
        if sha256_file(self.runtime/'sunshine.exe') != HOST_SHA:
            raise RuntimeError('전송 서버 파일이 검증한 버전과 다릅니다. 원본을 보존하고 시작을 멈췄습니다.')
        if self.preferences['depth_model'] == DEFAULT_DEPTH_MODEL:
            verified_model()
        else:
            verified_model(model_id=self.preferences['depth_model'])
        self._scan_sources()
        selected = next((m for m in self.get_snapshot()['monitors'] if m['device_name'] == self.preferences['monitor_device']), None)
        if not selected:
            raise RuntimeError('선택한 모니터가 연결되어 있지 않습니다. 모니터를 다시 선택해 주세요.')
        if abs(selected.get('width',1920)/selected.get('height',1080)-16/9) > .01:
            raise RuntimeError('현재 검증된 송출은 16:9입니다. 화면이 늘어나지 않도록 시작을 멈췄습니다. 16:9 모니터를 선택해 주세요.')
        return selected

    def _producer_argv(self, monitor, session, *, hdr):
        eye_width, eye_height = OUTPUT_PROFILES[self.preferences['output_profile']]
        args = [str(self.python), '-m', 'quest3d.cli', 'serve', '--monitor', str(monitor['index']),
                '--bridge-protocol', '2', '--mode', self.preferences['mode'], '--disparity',
                str(self.preferences['depth_percent']*eye_width/100), '--fps', '60', '--ai-size',
                str(AI_QUALITY_PROFILES[validate_ai_quality(self.preferences['ai_quality'])]),
                '--depth-execution', 'cuda-graph', '--reuse-depth-constants', '--fused-depth-resize',
                '--fused-stereo-output', '--fused-forward-validation', '--fused-colour-fit',
                '--reuse-immutable-payload',
                '--eye-width', str(eye_width), '--eye-height', str(eye_height), '--resize-filter', 'bicubic-aa',
                '--stereo-method', 'forward-cuda', '--depth-refinement', 'none', '--colour-precision', 'float',
                '--disparity-profile', self.preferences['profile'], '--output', str(session)]
        args.extend(['--depth-model', self.preferences['depth_model']])
        if all(row['available'] for row in self._depth_model_options()):
            args.append('--enable-dad-comparison')
        if hdr:
            args.extend(['--experimental-hdr', '--hdr-tonemap', 'fused'])
        return args

    def _wait_ready(self, session, timeout=40):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            path = session/'status.json'
            if path.exists():
                status = read_json(path)
                if status.get('error') or status.get('ai_error'):
                    raise RuntimeError('영상 처리 시작 실패: '+str(status.get('error') or status.get('ai_error')))
                if not status.get('running'):
                    raise RuntimeError('영상 처리가 시작 중 종료됐습니다. 로그를 확인해 주세요.')
                if status.get('published_frames', 0) >= 2 and status.get('ai_ready'):
                    if self.preferences['mode'] == '2d' or status.get('effective_mode') == ('2d' if self.preferences['depth_percent'] == 0 else '3d'):
                        return status
            time.sleep(.2)
        raise RuntimeError('영상 준비가 40초 안에 완료되지 않았습니다. 로그를 확인해 주세요.')

    def _start(self):
        self._check_pending_helper()
        self._set(phase='starting', message='실행 환경과 저장된 화질 설정을 확인하고 있습니다.')
        self._inspect()
        snapshot = self.get_snapshot()
        if snapshot['running'] and (not snapshot['ready'] or snapshot['phase'] == 'conflict'):
            raise RuntimeError(snapshot['error'] or '현재 영상 처리가 준비되지 않았습니다. 중지 후 다시 시작해 주세요.')
        if self._producer and self._host:
            self._verify_host_ready()
            self._set(message='현재 실행을 그대로 연결했습니다.')
            return
        if self._host:
            raise RuntimeError('전송 서버가 다른 상태로 실행 중입니다. 중지 후 다시 시작해 주세요.')
        self._recover_audio()
        audio_output.plan(self.preferences['audio_output'], root=self.root)
        monitor = self._preflight()
        self._last_log_dir = self.logs/('run-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        self._last_log_dir.mkdir()
        if not self._producer:
            from .display_color import read_display_colors, require_hdr_color
            matches = [c for c in read_display_colors() if c.device_name.casefold() == monitor['device_name'].casefold()]
            if len(matches) != 1 or matches[0].hdr_enabled is None:
                raise RuntimeError('모니터의 HDR/SDR 상태를 확인하지 못했습니다. Windows 화면 설정을 확인해 주세요.')
            hdr = matches[0].hdr_enabled
            if hdr:
                require_hdr_color(monitor['device_name'], matches)
            session = self.root/'artifacts'/('session-desktop-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
            self._session = session
            self._set(message='화면을 캡처하고 로컬 AI를 준비하고 있습니다.')
            argv = self._producer_argv(monitor, session, hdr=hdr)
            with (self._last_log_dir/'producer.log').open('wb') as output:
                child = subprocess.Popen(argv, cwd=self.root, env=self._environment(), stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT, creationflags=hidden_flags())
            self._pending = {'directory': str(session), 'launcher_identity': identity(child.pid)}
            atomic_json(self._pending_path, self._pending)
            atomic_json(self._last_log_dir/'launch.json', {'arguments': argv, **self._pending})
            try:
                self._wait_ready(session)
            except Exception:
                # Keep a recoverable identity/status; Stop remains available, no force-kill.
                self._producer, _ = self._find_producer(session)
                raise
            self._producer, _ = self._find_producer(session)
            if not self._producer:
                raise RuntimeError('영상 처리의 실제 프로세스 신원을 확인하지 못했습니다.')
        self._set(message='전송 서버를 실제 영상에 연결하고 있습니다.')
        if self.distributed and not (self.root/'artifacts/host/dev/launch.json').exists():
            self._run([self.pwsh, '-NoProfile', '-File', self.root/'native/host/prepare-installed-host.ps1',
                       '-ControlDirectory', self._session, '-ExpectedRuntime', self.runtime,
                       '-ExpectedHostSha256', HOST_SHA], 'host-setup')
        command = [self.pwsh, '-NoProfile', '-File', self.root/'native/host/rebind-dev-source.ps1',
                   '-ControlDirectory', self._session, '-ExpectedRuntime', self.runtime, '-ExpectedHostSha256', HOST_SHA]
        audio_args = ['-AudioOutput', self.preferences['audio_output']] if self.preferences['audio_output'] != 'pc' else []
        command += audio_args
        self._run(command+['-CheckOnly'], 'host-check')
        self._run(command, 'host-bind')
        self._run([self.pwsh, '-NoProfile', '-File', self.root/'native/host/run-dev-host.ps1', '-Background'] + audio_args, 'host-start')
        self._host = self._host_identity()
        if not self._host:
            raise RuntimeError('전송 서버가 시작 후 종료됐습니다. 로그를 확인해 주세요.')
        self._verify_host_ready()
        self._set(phase='running', error=None, message='PC 영상이 준비됐습니다. Quest 앱에서 이 PC에 연결해 주세요.')
        self._event('started', producer=self._producer, host=self._host, session=str(self._session))

    def _pair(self, pin):
        """Submit once to the single freshly observed request; no secrets in logs."""
        if not isinstance(pin, str) or len(pin) != 4 or any(c not in '0123456789' for c in pin):
            raise ValueError('Quest에 표시된 숫자 네 자리를 입력해 주세요.')
        self._inspect()
        snapshot = self.get_snapshot()
        if not self._host or not snapshot['ready'] or snapshot['phase'] == 'conflict' or snapshot['error']:
            raise RuntimeError('PC 영상이 정상으로 준비된 뒤 Quest에서 새 PC 연결을 눌러 주세요.')
        self._verify_host_ready()
        from .desktop_pairing import approve_pin
        self._set(pairing_message='Quest 연결 요청을 확인하고 있습니다.')
        result = approve_pin(self.root, self.pwsh, self._environment(), pin)
        if not result.get('host_accepted'):
            raise RuntimeError('PIN을 승인하지 못했습니다. Quest의 새 PIN으로 다시 시도해 주세요.')
        self._set(pairing_message='PIN을 승인했습니다. Quest에서 연결 완료를 확인해 주세요.')
        self._event('pair_accepted', quest_address=result.get('quest_address'))

    def _stop(self):
        self._check_pending_helper()
        self._set(phase='stopping', message='현재 설정을 저장하고 송출을 정상 종료하고 있습니다.')
        self._inspect()
        if self._host:
            expected = dict(self._host)
            if not same_process(expected) or sha256_file(self.runtime/'sunshine.exe') != HOST_SHA:
                raise RuntimeError('전송 서버의 신원이 바뀌어 종료하지 않았습니다.')
            self._run([self.python, self.root/'scripts/stop-verified-host.py', '--exe', self.runtime/'sunshine.exe',
                       '--pid', str(expected['pid']), '--birth', expected['birth']], 'host-stop', timeout=20)
            if same_process(expected):
                raise RuntimeError('전송 서버가 아직 종료 중입니다. 강제 종료하지 않았습니다.')
            self._host = None
        self._recover_audio()
        if self._producer:
            expected = dict(self._producer)
            if not same_process(expected):
                raise RuntimeError('영상 처리 프로세스의 신원이 바뀌었습니다. 다시 확인해 주세요.')
            status = read_json(self._session/'status.json')
            request_id = None
            if status.get('running'):
                request_id = send_control(self._session, stop=True)['request_id']
            deadline = time.monotonic()+18
            while same_process(expected) and time.monotonic() < deadline:
                time.sleep(.15)
            if same_process(expected):
                raise RuntimeError('영상 처리가 아직 종료되지 않았습니다. 진단을 저장한 뒤 다시 시도해 주세요. 강제 종료는 하지 않았습니다.')
            self._producer = None
            final = read_json(self._session/'status.json')
            if (final.get('session_id') != status['session_id'] or final.get('running')
                    or (request_id and final.get('seen_request') != request_id)
                    or final.get('error') or final.get('ai_error') or final.get('cleanup_errors')):
                raise RuntimeError('프로세스는 종료됐지만 정상 정리 완료를 확인하지 못했습니다. 진단 기록을 확인해 주세요.')
        elif self._pending and same_process(self._pending.get('launcher_identity')):
            raise RuntimeError('영상 준비 도구가 아직 시작 중입니다. 상태 파일이 준비된 뒤 중지를 다시 눌러 주세요.')
        self._pending = None
        self._pending_path.unlink(missing_ok=True)
        self._last_counter = None
        self._save_preferences()
        self._set(phase='idle', error=None, running=False, ready=False, host_running=False, message='송출을 중지했습니다. 다시 시작할 수 있습니다.')
        self._event('stopped')

    def _control(self, mode=None, depth_percent=None, depth_delta=None, profile=None, depth_model=None):
        self._inspect()
        if not self._producer or not same_process(self._producer):
            raise RuntimeError('PC 영상을 먼저 시작해 주세요.')
        before = read_json(self._session/'status.json')
        age = (time.perf_counter_ns()-before['updated_monotonic_ns'])/1e9
        if not before.get('running') or not 0 <= age <= 4 or before.get('error') or before.get('ai_error'):
            raise RuntimeError('영상 상태가 준비되지 않았습니다. 상태를 새로고침해 주세요.')
        values = {}
        if mode is not None:
            if mode not in ('2d', '3d'):
                raise ValueError('보기 모드가 올바르지 않습니다.')
            values['mode'] = mode
        if depth_delta is not None:
            if depth_percent is not None or depth_delta not in (-.05, .05):
                raise ValueError('미세 조절 간격이 올바르지 않습니다.')
            depth_percent = min(4., max(0., before['disparity']/before['eye_width']*100+depth_delta))
        if depth_percent is not None:
            if isinstance(depth_percent, bool) or not isinstance(depth_percent, (float,int)) or not math.isfinite(depth_percent) or not 0 <= depth_percent <= 4:
                raise ValueError('Depth는 0~4% 범위의 숫자여야 합니다.')
            values['disparity'] = depth_percent*before['eye_width']/100
        if profile is not None:
            if profile not in ('linear', 'comfort'):
                raise ValueError('윤곽 보정 설정이 올바르지 않습니다.')
            values['disparity_profile'] = profile
        if depth_model is not None:
            validate_depth_model(depth_model)
            if depth_model not in before.get('available_depth_models', []):
                raise RuntimeError('모델 비교 준비 필요 · PC 중지 후 다시 시작')
            values['depth_model'] = depth_model
        # Derive values AND CAS revision from the very same observation. A Quest
        # update between here and application must reject this request, not lose it.
        request = dict(session_id=before['session_id'], request_id=uuid.uuid4().hex,
                       mode=before['requested_mode'], disparity=before['disparity'],
                       expected_revision=before['revision'])
        request.update(values)
        validate_request(request, before['session_id'], before['eye_width'])
        atomic_json(self._session/'request.json', request)
        result = request
        deadline = time.monotonic()+12
        while time.monotonic() < deadline:
            status = read_json(self._session/'status.json')
            if status.get('applied_request') == result['request_id']:
                self._set(error=None, message='설정이 실제 영상에 적용됐습니다.')
                self._inspect()
                return
            if status.get('rejected_request') == result['request_id']:
                raise RuntimeError('Quest에서 설정이 바뀌었거나 요청이 거절됐습니다. 현재값을 확인하고 다시 조절해 주세요.')
            time.sleep(.06)
        raise RuntimeError('설정 적용 응답을 확인하지 못했습니다. 현재 영상과 상태를 확인한 뒤 다시 시도해 주세요.')

    def _select_depth_model(self, depth_model):
        validate_depth_model(depth_model)
        if self._prefs_error:
            raise RuntimeError(self._prefs_error)
        self._inspect()
        option = next(row for row in self._depth_model_options() if row['id'] == depth_model)
        if not option['available']:
            raise RuntimeError('모델 파일 확인 필요 · 설치 또는 복구 후 선택')
        if self.get_snapshot()['running']:
            return self._control(depth_model=depth_model)
        if self._host:
            raise RuntimeError('전송 상태 확인 필요 · PC 중지 후 선택')
        self.preferences['depth_model'] = depth_model
        self._save_preferences()
        self._set(depth_model=depth_model, depth_models=self._depth_model_options(), error=None)

    def _select_output_profile(self, output_profile):
        """Change next-start geometry only after all owned streaming work stops."""
        if not isinstance(output_profile, str) or output_profile not in OUTPUT_PROFILES:
            raise ValueError('Headset은 Quest 2 또는 Quest 3 선택')
        if self._prefs_error:
            raise RuntimeError(self._prefs_error)
        self._check_pending_helper()
        self._inspect()
        if self.get_snapshot()['running'] or self._host:
            raise RuntimeError('Headset 변경 전 PC 송출 중지 필요')
        self.preferences['output_profile'] = output_profile
        self._save_preferences()
        self._set(output_profile=output_profile, eye_text=output_eye_text(output_profile), error=None)

    def _select_ai_quality(self, ai_quality):
        """A size change needs fresh graphs, created before the capture starts."""
        validate_ai_quality(ai_quality)
        if self._prefs_error:
            raise RuntimeError(self._prefs_error)
        self._check_pending_helper()
        self._inspect()
        snapshot = self.get_snapshot()
        if snapshot['running'] or self._host or snapshot['phase'] not in ('idle', 'error'):
            raise RuntimeError('AI Quality 변경 전 PC 송출 중지 필요')
        previous = self.preferences['ai_quality']
        self.preferences['ai_quality'] = ai_quality
        try:
            self._save_preferences()
        except Exception:
            self.preferences['ai_quality'] = previous
            raise
        self._set(**ai_quality_status(ai_quality), error=None)

    def _select_audio_output(self, audio_output):
        from .audio_output import plan, validate_output, OUTPUTS
        validate_output(audio_output)
        if self._prefs_error:
            raise RuntimeError(self._prefs_error)
        self._check_pending_helper()
        self._inspect()
        snapshot = self.get_snapshot()
        if snapshot['running'] or self._host or snapshot['phase'] not in ('idle', 'error'):
            raise RuntimeError('Sound 변경 전 PC 송출 중지 필요')
        self._recover_audio()
        plan(audio_output, root=self.root)
        previous = self.preferences['audio_output']
        self.preferences['audio_output'] = audio_output
        try:
            self._save_preferences()
        except Exception:
            self.preferences['audio_output'] = previous
            raise
        self._audio_options_cache = None
        self._set(audio_output=audio_output, active_audio_output=None,
                  audio_status='다음 시작 · ' + OUTPUTS[audio_output], error=None)

    def _select_monitor(self, device_name):
        self._inspect()
        if self._producer or self._host:
            raise RuntimeError('모니터를 바꾸려면 송출을 먼저 중지해 주세요.')
        self._scan_sources()
        if device_name not in [m['device_name'] for m in self.get_snapshot()['monitors']]:
            raise RuntimeError('선택한 모니터가 더 이상 연결되어 있지 않습니다.')
        self.preferences['monitor_device'] = device_name
        self._save_preferences()
        self._set(selected_monitor=device_name, error=None)

    def _refresh(self):
        self._last_discovery = self._last_source_scan = 0
        self._audio_options_cache = None
        self._set(error=None)

    def _open_logs(self):
        os.startfile(self._last_log_dir)

    def _open_guide(self):
        os.startfile(self.root/'docs/DESKTOP_USER_GUIDE.html')

    def _open_diagnostics(self):
        target = self.get_snapshot().get('last_diagnostics')
        os.startfile(target or self.logs)

    def _export_diagnostics(self):
        destination = self.logs/('diagnostics-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        destination.mkdir()
        # Deliberate allowlist: no credentials, pairing keys, screenshots or media.
        atomic_json(destination/'desktop.json', {'state': self.get_snapshot(), 'preferences': self.preferences,
                                                'producer': self._producer, 'host': self._host})
        if self._status:
            atomic_json(destination/'status.json', self._status)
        for name, path in [('host.log', self.root/'artifacts/host/dev/sunshine.log'),
                           ('desktop-events.jsonl', self.logs/'events.jsonl'),
                           ('producer.log', self._last_log_dir/'producer.log')]:
            (destination/name).write_text(bounded_tail(path, 65536), encoding='utf-8')
        self._set(last_diagnostics=str(destination), message='진단 기록을 저장했습니다. 진단 폴더 열기로 확인할 수 있습니다.')
        self._event('diagnostics_saved', directory=str(destination))
