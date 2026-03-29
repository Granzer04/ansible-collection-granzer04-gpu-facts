#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r'''
---
module: gpu_facts
short_description: Gather GPU hardware and driver facts
description:
  - Detects GPUs using vendor driver tools when available.
  - Falls back to hardware-level scanning when driver tools are unavailable.
  - Supports Linux, Windows, and macOS with a normalized output schema.
version_added: "0.1.0"
author:
  - Granzer04
options: {}
notes:
  - Module is read-only and always returns changed=false.
  - On Linux, fallback hardware scan uses C(lspci) if present.
'''

EXAMPLES = r'''
- name: Gather GPU facts
  granzer04.gpu_facts.gpu_facts:

- name: Show detected GPUs
  ansible.builtin.debug:
    var: ansible_facts.gpus
'''

RETURN = r'''
ansible_facts:
  description: Facts injected into the host ansible_facts namespace.
  returned: always
  type: dict
  contains:
    gpus:
      description: List of detected GPU devices.
      type: list
      elements: dict
    gpu_count:
      description: Total number of GPUs detected.
      type: int
    gpu_detection_errors:
      description: Non-fatal detection warnings.
      type: list
      elements: str
'''

import json
import platform
import re

from ansible.module_utils.basic import AnsibleModule


_PCI_VENDOR_MAP = {
    '10DE': 'nvidia',
    '1002': 'amd',
    '8086': 'intel',
}


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
    )


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
        '  $result += [PSCustomObject]@{ '
        '    Name=$d.FriendlyName; '
        '    InstanceId=$d.InstanceId; '
        '    HardwareIds=$hw; '
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

        name = reported_name
        if _is_generic_windows_display_name(name) and vendor != 'unknown':
            suffix = f' ({device_id})' if device_id else ''
            name = vendor.upper() + ' GPU' + suffix

        gpu = _empty_gpu(len(gpus), name=name, vendor=vendor, method='windows-pnp')
        gpu['pci_id'] = instance_id
        gpu['pci_vendor_id'] = vendor_id
        gpu['pci_device_id'] = device_id
        gpu['hardware_ids'] = hardware_ids
        gpu['reported_name'] = reported_name
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
            gpu = _empty_gpu(len(gpus), name=reported_name, vendor=vendor, method='windows-wmi')
            gpu['pci_id'] = row.get('PNPDeviceID')
            gpu['pci_vendor_id'] = vendor_id
            gpu['pci_device_id'] = device_id
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
