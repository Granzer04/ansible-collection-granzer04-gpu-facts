#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r'''
---
module: gpu_facts
short_description: Gather GPU hardware and driver facts
description:
    - Collects GPU inventory and best-effort driver details using a driver-first, hardware-fallback strategy.
    - On Linux, tries vendor utilities first and falls back to C(lspci) hardware scanning when needed.
    - On Windows, queries Plug and Play display devices first, then enriches them with C(Win32_VideoController) data.
    - On macOS, gathers display controller data from C(system_profiler).
    - Returns a normalized schema so playbooks can consume one GPU fact structure across supported operating systems.
version_added: "0.1.0"
author:
    - Granzer04
options: {}
notes:
    - Module is read-only and always returns changed=false.
    - Missing vendor utilities are non-fatal and are reported in C(gpu_detection_errors).
    - On Linux, vendor utilities currently include C(nvidia-smi), C(rocm-smi), and C(xpu-smi).
    - On Linux, fallback hardware scan uses C(lspci) if present.
    - On Windows, the module reads display adapters from C(Get-PnpDevice -Class Display) and enriches matching devices with C(Win32_VideoController).
    - On Windows, generic names such as C(Microsoft Basic Display Adapter) may be replaced with a bus-reported device description, a device description, or a PCI vendor/device lookup result.
    - On Windows, PCI-based vendor detection can still work even when no vendor driver utility is installed.
    - The exact fields populated for each GPU depend on what the host operating system exposes.
'''

EXAMPLES = r'''
- name: Gather GPU facts
    granzer04.gpu_facts.gpu_facts:

- name: Show detected GPUs
    ansible.builtin.debug:
        var: ansible_facts.gpus

- name: Show the first detected GPU
    ansible.builtin.debug:
        var: ansible_facts.gpus[0]
    when: ansible_facts.gpu_count | int > 0

- name: Show non-fatal GPU detection warnings
    ansible.builtin.debug:
        var: ansible_facts.gpu_detection_errors
    when: ansible_facts.gpu_detection_errors | length > 0

- name: Show Windows-oriented identification fields
    ansible.builtin.debug:
        msg:
            name: "{{ item.name }}"
            reported_name: "{{ item.reported_name | default('') }}"
            resolved_name: "{{ item.resolved_name | default('') }}"
            pci_vendor_id: "{{ item.pci_vendor_id | default('') }}"
            pci_device_id: "{{ item.pci_device_id | default('') }}"
    loop: "{{ ansible_facts.gpus }}"
    when: ansible_system == 'Win32NT'
'''

RETURN = r'''
ansible_facts:
    description: Facts injected into the host ansible_facts namespace.
    returned: always
    type: dict
    contains:
        gpus:
            description: List of detected GPU devices in a normalized schema.
            type: list
            elements: dict
            contains:
                index:
                    description: Zero-based GPU index in the final merged result.
                    type: int
                name:
                    description: Best available display name for the GPU after fallback resolution.
                    type: str
                vendor:
                    description: Normalized vendor name such as C(nvidia), C(amd), C(intel), or C(unknown).
                    type: str
                driver_detected:
                    description: Whether a driver version or vendor utility result was found for this GPU.
                    type: bool
                driver_version:
                    description: Driver version reported by a vendor utility or operating system query when available.
                    type: str
                vram_mb:
                    description: Total adapter memory in MiB when available.
                    type: int
                vram_free_mb:
                    description: Free adapter memory in MiB when available from vendor tooling.
                    type: int
                temperature_c:
                    description: GPU temperature in Celsius when available from vendor tooling.
                    type: int
                utilization_pct:
                    description: GPU utilization percentage when available from vendor tooling.
                    type: int
                pci_id:
                    description: Platform-specific bus or device identifier for the GPU.
                    type: str
                uuid:
                    description: Stable device UUID when reported by the platform or vendor utility.
                    type: str
                detection_method:
                    description: Source that produced the GPU record, such as C(nvidia-smi), C(lspci), C(windows-pnp), or C(windows-wmi).
                    type: str
                pci_vendor_id:
                    description: PCI vendor identifier extracted from Windows device IDs when available.
                    type: str
                pci_device_id:
                    description: PCI device identifier extracted from Windows device IDs when available.
                    type: str
                hardware_ids:
                    description: Raw Windows hardware ID list returned by Plug and Play when available.
                    type: list
                    elements: str
                reported_name:
                    description: Original Windows-reported display adapter name before any fallback renaming.
                    type: str
                bus_reported_name:
                    description: Windows bus-reported device description used as a higher-quality fallback name when available.
                    type: str
                device_description:
                    description: Windows device description read from Plug and Play properties when available.
                    type: str
                resolved_name:
                    description: Name resolved from PCI vendor/device lookup data when generic Windows naming needs replacement.
                    type: str
                status:
                    description: Windows Plug and Play device status when available.
                    type: str
                problem_code:
                    description: Windows Plug and Play problem code when available.
                    type: int
        gpu_count:
            description: Total number of GPUs detected.
            type: int
        gpu_detection_errors:
            description: Non-fatal detection warnings for missing tools, failed queries, or unsupported fallback paths.
            type: list
            elements: str
'''

import json
import os
import platform
import re

from ansible.module_utils.basic import AnsibleModule


_PCI_VENDOR_MAP = {
    '10DE': 'nvidia',
    '1002': 'amd',
    '8086': 'intel',
}

_PCI_LOOKUP_CACHE = None


def _safe_int(value, default=None):
    if value is None:
        return default
    try:
        return int(str(value).strip().split('.')[0])
    except (ValueError, TypeError, AttributeError):
        return default


def _run(module, cmd):
    executable = cmd[0]
    if hasattr(module, 'get_bin_path'):
        resolved = module.get_bin_path(executable, required=False)
        if not resolved:
            return 1, '', executable + ' not found'
        cmd = [resolved] + list(cmd[1:])
    try:
        rc, out, err = module.run_command(cmd, check_rc=False)
        return rc, (out or '').strip(), (err or '').strip()
    except Exception as exc:  # pragma: no cover
        return 1, '', str(exc)


def _vendor_from_name(name):
    text = (name or '').lower()
    if any(k in text for k in ('nvidia', 'geforce', 'quadro', 'tesla', 'rtx', 'gtx')):
        return 'nvidia'
    if any(k in text for k in ('amd', 'radeon', 'vega', 'firepro', 'instinct')):
        return 'amd'
    if any(k in text for k in ('intel', 'iris', 'uhd', 'arc', 'hd graphics')):
        return 'intel'
    return 'unknown'


def _vendor_from_pci_vendor_id(vendor_id):
    if not vendor_id:
        return 'unknown'
    return _PCI_VENDOR_MAP.get(str(vendor_id).upper(), 'unknown')


def _extract_pci_ids(value):
    text = str(value or '')
    ven = re.search(r'VEN_([0-9A-Fa-f]{4})', text)
    dev = re.search(r'DEV_([0-9A-Fa-f]{4})', text)
    vendor_id = ven.group(1).upper() if ven else None
    device_id = dev.group(1).upper() if dev else None
    return vendor_id, device_id


def _normalize_windows_records(raw):
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    return []


def _normalize_hardware_ids(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw if item is not None]
    return []


def _is_generic_windows_display_name(name):
    text = (name or '').lower()
    return (
        'microsoft basic display adapter' in text
        or 'basic display adapter' in text
        or text.strip() in ('display adapter', 'video controller')
        or 'video controller (vga compatible)' in text
        or 'vga compatible controller' in text
    )


def _read_json_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _load_repo_pci_lookup():
    base = os.path.dirname(__file__)
    lookup_path = os.path.normpath(os.path.join(base, '..', 'module_utils', 'pci_gpu_map.json'))
    raw = _read_json_file(lookup_path)
    if not isinstance(raw, dict):
        return {}

    result = {}
    for vendor_id, devices in raw.items():
        if not isinstance(devices, dict):
            continue
        v = str(vendor_id).upper()
        result[v] = {str(k).upper(): str(val) for k, val in devices.items()}
    return result


def _load_linux_pci_ids_lookup():
    candidates = [
        '/usr/share/misc/pci.ids',
        '/usr/share/hwdata/pci.ids',
    ]
    path = None
    for candidate in candidates:
        if os.path.exists(candidate):
            path = candidate
            break
    if not path:
        return {}

    lookup = {}
    current_vendor_id = None
    current_vendor_name = None

    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for raw_line in f:
                line = raw_line.rstrip('\n')
                if not line or line.startswith('#'):
                    continue
                if not line.startswith('\t'):
                    parts = line.split(None, 1)
                    if len(parts) != 2 or len(parts[0]) != 4:
                        current_vendor_id = None
                        current_vendor_name = None
                        continue
                    current_vendor_id = parts[0].upper()
                    current_vendor_name = parts[1].strip()
                    lookup.setdefault(current_vendor_id, {'_vendor_name': current_vendor_name})
                    continue
                if line.startswith('\t\t') or current_vendor_id is None:
                    continue

                device_parts = line.strip().split(None, 1)
                if len(device_parts) != 2 or len(device_parts[0]) != 4:
                    continue
                device_id = device_parts[0].upper()
                device_name = device_parts[1].strip()
                vendor_label = lookup[current_vendor_id].get('_vendor_name', current_vendor_id)
                lookup[current_vendor_id][device_id] = vendor_label + ' ' + device_name
    except Exception:
        return {}

    return lookup


def _get_pci_lookup():
    global _PCI_LOOKUP_CACHE
    if _PCI_LOOKUP_CACHE is not None:
        return _PCI_LOOKUP_CACHE

    lookup = _load_linux_pci_ids_lookup()
    repo_lookup = _load_repo_pci_lookup()

    # Repo table fills gaps and also supports non-Linux hosts.
    for vendor_id, devices in repo_lookup.items():
        lookup.setdefault(vendor_id, {})
        for device_id, device_name in devices.items():
            if device_id not in lookup[vendor_id]:
                lookup[vendor_id][device_id] = device_name

    _PCI_LOOKUP_CACHE = lookup
    return _PCI_LOOKUP_CACHE


def _resolve_name_from_pci_ids(vendor_id, device_id):
    if not vendor_id or not device_id:
        return None
    lookup = _get_pci_lookup()
    vendor_devices = lookup.get(str(vendor_id).upper())
    if not isinstance(vendor_devices, dict):
        return None
    return vendor_devices.get(str(device_id).upper())


def _empty_gpu(index, name='Unknown GPU', vendor='unknown', method='unknown'):
    return {
        'index': index,
        'name': name,
        'vendor': vendor,
        'driver_detected': False,
        'driver_version': None,
        'vram_mb': None,
        'vram_free_mb': None,
        'temperature_c': None,
        'utilization_pct': None,
        'pci_id': None,
        'uuid': None,
        'detection_method': method,
    }


def _detect_nvidia_smi(module, errors):
    fields = [
        'index',
        'name',
        'driver_version',
        'memory.total',
        'memory.free',
        'temperature.gpu',
        'utilization.gpu',
        'pci.bus_id',
        'uuid',
    ]
    rc, out, err = _run(
        module,
        ['nvidia-smi', '--query-gpu=' + ','.join(fields), '--format=csv,noheader,nounits'],
    )
    if rc != 0:
        errors.append('nvidia-smi unavailable: ' + (err or 'not found'))
        return []

    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) != len(fields):
            continue
        info = dict(zip(fields, parts))
        gpus.append(
            {
                'index': _safe_int(info.get('index'), len(gpus)),
                'name': info.get('name', 'NVIDIA GPU'),
                'vendor': 'nvidia',
                'driver_detected': True,
                'driver_version': info.get('driver_version'),
                'vram_mb': _safe_int(info.get('memory.total')),
                'vram_free_mb': _safe_int(info.get('memory.free')),
                'temperature_c': _safe_int(info.get('temperature.gpu')),
                'utilization_pct': _safe_int(info.get('utilization.gpu')),
                'pci_id': re.sub(r'^00000000:', '', info.get('pci.bus_id', '')),
                'uuid': info.get('uuid'),
                'detection_method': 'nvidia-smi',
            }
        )
    return gpus


def _detect_rocm_smi(module, errors):
    rc, out, err = _run(module, ['rocm-smi', '--showallinfo', '--json'])
    if rc != 0:
        errors.append('rocm-smi unavailable: ' + (err or 'not found'))
        return []

    try:
        data = json.loads(out)
    except ValueError:
        errors.append('rocm-smi returned non-JSON output')
        return []

    gpus = []
    for card_name in sorted(data.keys()):
        card = data.get(card_name)
        if not isinstance(card, dict):
            continue
        name = card.get('Card Series') or card.get('Card model') or 'AMD GPU'
        gpu = _empty_gpu(len(gpus), name=name, vendor='amd', method='rocm-smi')
        gpu['driver_detected'] = True
        gpu['driver_version'] = card.get('Driver version') or card.get('Driver Version')
        gpus.append(gpu)
    return gpus


def _detect_xpu_smi(module, errors):
    rc, out, err = _run(module, ['xpu-smi', 'discovery', '-j'])
    if rc != 0:
        errors.append('xpu-smi unavailable: ' + (err or 'not found'))
        return []

    try:
        data = json.loads(out)
    except ValueError:
        errors.append('xpu-smi returned non-JSON output')
        return []

    gpus = []
    for device in data.get('device_list', []):
        name = device.get('device_name', 'Intel GPU')
        gpu = _empty_gpu(len(gpus), name=name, vendor='intel', method='xpu-smi')
        gpu['driver_detected'] = True
        gpu['driver_version'] = device.get('driver_version')
        gpus.append(gpu)
    return gpus


def _scan_linux_lspci(module, errors):
    rc, out, err = _run(module, ['lspci'])
    if rc != 0:
        errors.append('lspci unavailable: ' + (err or 'not found'))
        return []

    gpus = []
    for line in out.splitlines():
        lower = line.lower()
        if 'vga compatible controller' not in lower and '3d controller' not in lower:
            continue
        name = line.split(':', 2)[-1].strip() if ':' in line else line.strip()
        vendor = _vendor_from_name(name)
        gpu = _empty_gpu(len(gpus), name=name, vendor=vendor, method='lspci')
        if ':' in line:
            gpu['pci_id'] = line.split(' ', 1)[0]
        gpus.append(gpu)
    return gpus


def _scan_windows_pnp(module, errors):
    script = (
        '$devices = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue; '
        '$result = @(); '
        'foreach ($d in $devices) { '
        '  $hw = (Get-PnpDeviceProperty -InstanceId $d.InstanceId '
        "    -KeyName 'DEVPKEY_Device_HardwareIds' -ErrorAction SilentlyContinue).Data; "
        '  $bus = (Get-PnpDeviceProperty -InstanceId $d.InstanceId '
        "    -KeyName 'DEVPKEY_Device_BusReportedDeviceDesc' -ErrorAction SilentlyContinue).Data; "
        '  $desc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId '
        "    -KeyName 'DEVPKEY_Device_DeviceDesc' -ErrorAction SilentlyContinue).Data; "
        '  $result += [PSCustomObject]@{ '
        '    Name=$d.FriendlyName; '
        '    InstanceId=$d.InstanceId; '
        '    HardwareIds=$hw; '
        '    BusReportedDeviceDesc=$bus; '
        '    DeviceDesc=$desc; '
        '    Status=$d.Status; '
        '    ProblemCode=$d.Problem '
        '  }; '
        '} '
        'if ($result.Count -eq 0) { $result = @() } '
        'ConvertTo-Json -InputObject $result -Depth 4'
    )
    rc, out, err = _run(module, ['powershell', '-NonInteractive', '-Command', script])
    if rc != 0:
        errors.append('Windows PnP query failed: ' + (err or 'not available'))
        return []

    try:
        data = json.loads(out) if out else []
    except ValueError:
        errors.append('Windows PnP output is not valid JSON')
        return []

    records = _normalize_windows_records(data)
    gpus = []
    for row in records:
        reported_name = row.get('Name') or 'Windows GPU'
        instance_id = row.get('InstanceId')
        hardware_ids = _normalize_hardware_ids(row.get('HardwareIds'))
        vendor_id, device_id = _extract_pci_ids(' '.join(hardware_ids) + ' ' + str(instance_id or ''))
        vendor = _vendor_from_pci_vendor_id(vendor_id)
        bus_reported_name = row.get('BusReportedDeviceDesc')
        device_desc = row.get('DeviceDesc')
        resolved_name = _resolve_name_from_pci_ids(vendor_id, device_id)

        name = reported_name
        if _is_generic_windows_display_name(name):
            if bus_reported_name:
                name = bus_reported_name
            elif device_desc and not _is_generic_windows_display_name(device_desc):
                name = device_desc
            elif resolved_name:
                name = resolved_name
            elif vendor != 'unknown':
                suffix = f' ({device_id})' if device_id else ''
                name = vendor.upper() + ' GPU' + suffix

        gpu = _empty_gpu(len(gpus), name=name, vendor=vendor, method='windows-pnp')
        gpu['pci_id'] = instance_id
        gpu['pci_vendor_id'] = vendor_id
        gpu['pci_device_id'] = device_id
        gpu['hardware_ids'] = hardware_ids
        gpu['reported_name'] = reported_name
        gpu['bus_reported_name'] = bus_reported_name
        gpu['device_description'] = device_desc
        gpu['resolved_name'] = resolved_name
        gpu['status'] = row.get('Status')
        gpu['problem_code'] = row.get('ProblemCode')
        gpus.append(gpu)

    return gpus


def _scan_windows_wmi(module, errors):
    script = (
        'Get-CimInstance Win32_VideoController | '
        'Select-Object Name,AdapterRAM,DriverVersion,PNPDeviceID | '
        'ConvertTo-Json -Depth 3'
    )
    rc, out, err = _run(module, ['powershell', '-NonInteractive', '-Command', script])
    if rc != 0:
        errors.append('Windows WMI query failed: ' + (err or 'not available'))
        return []

    try:
        data = json.loads(out)
    except ValueError:
        errors.append('Windows WMI output is not valid JSON')
        return []

    records = _normalize_windows_records(data)
    result = {}
    for row in records:
        key = str(row.get('PNPDeviceID') or '').upper()
        if key:
            result[key] = row
    return result


def _scan_windows(module, errors):
    pnp_gpus = _scan_windows_pnp(module, errors)
    wmi_map = _scan_windows_wmi(module, errors)

    if not pnp_gpus and wmi_map:
        gpus = []
        for row in wmi_map.values():
            reported_name = row.get('Name') or 'Windows GPU'
            vendor_id, device_id = _extract_pci_ids(row.get('PNPDeviceID'))
            vendor = _vendor_from_pci_vendor_id(vendor_id)
            if vendor == 'unknown':
                vendor = _vendor_from_name(reported_name)
            resolved_name = _resolve_name_from_pci_ids(vendor_id, device_id)
            final_name = reported_name
            if _is_generic_windows_display_name(final_name) and resolved_name:
                final_name = resolved_name
            gpu = _empty_gpu(len(gpus), name=final_name, vendor=vendor, method='windows-wmi')
            gpu['pci_id'] = row.get('PNPDeviceID')
            gpu['pci_vendor_id'] = vendor_id
            gpu['pci_device_id'] = device_id
            gpu['resolved_name'] = resolved_name
            gpu['reported_name'] = reported_name
            driver_version = row.get('DriverVersion')
            gpu['driver_detected'] = bool(driver_version)
            gpu['driver_version'] = driver_version
            adapter_ram = _safe_int(row.get('AdapterRAM'))
            if adapter_ram and adapter_ram != 4294967295:
                gpu['vram_mb'] = adapter_ram // (1024 * 1024)
            gpus.append(gpu)
        return gpus

    for gpu in pnp_gpus:
        key = str(gpu.get('pci_id') or '').upper()
        row = wmi_map.get(key)
        if row is None and key:
            for candidate_key, candidate in wmi_map.items():
                if candidate_key and (candidate_key in key or key in candidate_key):
                    row = candidate
                    break
        if row is None:
            continue

        driver_version = row.get('DriverVersion')
        gpu['driver_detected'] = bool(driver_version)
        gpu['driver_version'] = driver_version
        adapter_ram = _safe_int(row.get('AdapterRAM'))
        if adapter_ram and adapter_ram != 4294967295:
            gpu['vram_mb'] = adapter_ram // (1024 * 1024)

        wmi_name = row.get('Name')
        if wmi_name and _is_generic_windows_display_name(gpu.get('name')) and not _is_generic_windows_display_name(wmi_name):
            gpu['name'] = wmi_name
        if gpu.get('vendor') == 'unknown' and wmi_name:
            gpu['vendor'] = _vendor_from_name(wmi_name)

    return pnp_gpus


def _parse_vram_mb(raw):
    if not raw:
        return None
    text = str(raw).strip()
    match = re.match(r'(\d+)\s*(GB|GiB|MB|MiB)', text, re.IGNORECASE)
    if not match:
        return None
    size = int(match.group(1))
    unit = match.group(2).lower()
    if unit in ('gb', 'gib'):
        return size * 1024
    return size


def _scan_macos_system_profiler(module, errors):
    rc, out, err = _run(module, ['system_profiler', 'SPDisplaysDataType', '-json'])
    if rc != 0:
        errors.append('system_profiler failed: ' + (err or 'not available'))
        return []

    try:
        data = json.loads(out)
    except ValueError:
        errors.append('system_profiler returned non-JSON output')
        return []

    gpus = []
    for display in data.get('SPDisplaysDataType', []):
        name = display.get('sppci_model') or display.get('_name') or 'macOS GPU'
        gpu = _empty_gpu(len(gpus), name=name, vendor=_vendor_from_name(name), method='system_profiler')
        gpu['vram_mb'] = _parse_vram_mb(display.get('sppci_vram') or display.get('sppci_vram_shared'))
        gpu['driver_detected'] = bool(display.get('sppci_driver_version'))
        gpu['driver_version'] = display.get('sppci_driver_version')
        gpu['pci_id'] = display.get('sppci_bus')
        gpus.append(gpu)
    return gpus


def _merge_gpus(primary, fallback):
    if not primary:
        return list(fallback)
    if not fallback:
        return list(primary)

    names = {gpu.get('name', '').lower() for gpu in primary}
    merged = list(primary)
    for gpu in fallback:
        if gpu.get('name', '').lower() not in names:
            merged.append(gpu)
    return merged


def gather_gpu_facts(module):
    errors = []
    driver_gpus = []

    driver_gpus.extend(_detect_nvidia_smi(module, errors))

    system = platform.system()
    if system == 'Linux':
        driver_gpus.extend(_detect_rocm_smi(module, errors))
        driver_gpus.extend(_detect_xpu_smi(module, errors))

    if system == 'Linux':
        fallback = _scan_linux_lspci(module, errors)
    elif system == 'Windows':
        fallback = _scan_windows(module, errors)
    elif system == 'Darwin':
        fallback = _scan_macos_system_profiler(module, errors)
    else:
        fallback = []
        errors.append('Unsupported OS for fallback scan: ' + system)

    gpus = _merge_gpus(driver_gpus, fallback)
    for idx, gpu in enumerate(gpus):
        gpu['index'] = idx

    return {
        'gpus': gpus,
        'gpu_count': len(gpus),
        'gpu_detection_errors': errors,
    }


def run_module():
    module = AnsibleModule(argument_spec={}, supports_check_mode=True)
    facts = gather_gpu_facts(module)
    module.exit_json(changed=False, ansible_facts=facts)


def main():
    run_module()


if __name__ == '__main__':
    main()
