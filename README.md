# granzer04.gpu_facts

An Ansible collection focused on OS-agnostic GPU detection facts.

## Goals

- Detect AMD, Intel, and NVIDIA GPUs.
- Work across Linux, Windows, and macOS.
- Prefer driver tooling when present.
- Fall back to hardware scan when drivers are missing.

## Collection Name

- Namespace: granzer04
- Collection: gpu_facts
- FQCN: granzer04.gpu_facts

## Module

- granzer04.gpu_facts.gpu_facts

## Windows Detection Strategy

For Windows hosts, the module now uses a two-stage fallback path designed for offline and no-driver scenarios:

1. PnP scan (primary)
- Query display devices with `Get-PnpDevice -Class Display`.
- Read hardware IDs with `Get-PnpDeviceProperty -KeyName DEVPKEY_Device_HardwareIds`.
- Extract PCI vendor/device identifiers from hardware IDs.

2. WMI enrichment (secondary)
- Query `Win32_VideoController` for `DriverVersion`, `AdapterRAM`, and controller name.
- Merge those values into the PnP result when IDs match.

3. Repository-backed PCI resolution (fallback)
- If Windows still returns a generic adapter label, the module attempts to resolve a model name from `VEN+DEV` using the repository mapping file:
- `plugins/module_utils/pci_gpu_map.json`
- This file is intentionally separate from code so it can be expanded over time without changing module logic.

This approach allows detection even when the host only reports generic names such as `Microsoft Basic Display Adapter`.

## PCI ID Coverage Strategy

Model resolution uses an offline source chain in this order:

1. Linux system PCI database (`/usr/share/misc/pci.ids` or `/usr/share/hwdata/pci.ids`)
2. Bundled repository PCI data (`plugins/module_utils/pci.ids`)
3. Curated override map (`plugins/module_utils/pci_gpu_map.json`)

The bundled source is generated from upstream `pci.ids` data for GPU vendors and refreshed at release time.

### Refresh bundled PCI IDs (maintainers)

Run from repository root:

```powershell
python scripts/refresh-pci-ids.py
```

Optional local source file:

```powershell
python scripts/refresh-pci-ids.py --source C:\path\to\pci.ids
```

By default, the refresh includes NVIDIA (`10DE`), AMD (`1002`), and Intel (`8086`) PCI vendor IDs.

## Release Quality Gates

Before tagging a release, validate all of the following:

1. Parser tests cover normal, malformed, class-section, and subsystem-line inputs.
2. Merge precedence tests pass for Linux system source, bundled snapshot, and curated overrides.
3. Linux fallback order regression passes (`lspci` primary, sysfs secondary only when `lspci` is unavailable).
4. Windows and macOS behavior tests remain green.
5. Unit test suite passes after PCI refresh updates.

## Development Setup

This repository supports two workflows:

1. Dev Container (primary on Windows 11)
2. Local Python virtual environment (fallback)

### Dev Container

Requirements:

- Docker Desktop
- VS Code Dev Containers extension

Open the repository in VS Code and run:

1. Command Palette
2. Dev Containers: Reopen in Container

### Local venv (fallback)

Use Python 3.11.

```powershell
scripts/setup-venv.ps1
```

## Local Validation

```powershell
scripts/validate-local.ps1
```

## Full Validation in Dev Container

Run these commands from inside the Dev Container:

```bash
ansible-galaxy collection build
ansible-test sanity plugins/modules/gpu_facts.py --test compile --test import --test ansible-doc -v
pytest tests/unit -v
```

## Example Playbook

```yaml
- name: Gather GPU facts
  hosts: all
  gather_facts: false
  tasks:
    - name: Collect GPU hardware information
      granzer04.gpu_facts.gpu_facts:

    - name: Show GPU facts
      ansible.builtin.debug:
        var: ansible_facts.gpus
```

## Example Result

### Linux fallback output (no vendor driver tools)

Example output from a Linux host where vendor driver tools are not installed and the module falls back to a hardware scan:

```yaml
ok: [localhost] => {
  "ansible_facts": {
    "gpu_count": 1,
    "gpu_detection_errors": [
      "nvidia-smi unavailable: nvidia-smi not found",
      "rocm-smi unavailable: rocm-smi not found",
      "xpu-smi unavailable: xpu-smi not found"
    ],
    "gpus": [
      {
        "detection_method": "lspci",
        "driver_detected": false,
        "driver_version": null,
        "index": 0,
        "name": "Intel Corporation UHD Graphics 770",
        "pci_id": "01:00.0",
        "temperature_c": null,
        "utilization_pct": null,
        "uuid": null,
        "vendor": "intel",
        "vram_free_mb": null,
        "vram_mb": null
      }
    ]
  },
  "changed": false
}
```

### Linux driver-tool output (AMD)

Example output when an AMD driver tool is available:

```yaml
ok: [localhost] => {
  "ansible_facts": {
    "gpu_count": 1,
    "gpu_detection_errors": [],
    "gpus": [
      {
        "detection_method": "rocm-smi",
        "driver_detected": true,
        "driver_version": "6.0.0",
        "index": 0,
        "name": "AMD Radeon RX 7900 XTX",
        "pci_id": "01:00.0",
        "temperature_c": null,
        "utilization_pct": null,
        "uuid": null,
        "vendor": "amd",
        "vram_free_mb": null,
        "vram_mb": 24576
      }
    ]
  },
  "changed": false
}
```

### Linux driver-tool output (NVIDIA)

Example output when an NVIDIA driver tool is available:

```yaml
ok: [localhost] => {
  "ansible_facts": {
    "gpu_count": 1,
    "gpu_detection_errors": [],
    "gpus": [
      {
        "detection_method": "nvidia-smi",
        "driver_detected": true,
        "driver_version": "555.12",
        "index": 0,
        "name": "NVIDIA GeForce RTX 4090",
        "pci_id": "01:00.0",
        "temperature_c": 40,
        "utilization_pct": 10,
        "uuid": "GPU-1234",
        "vendor": "nvidia",
        "vram_free_mb": 12000,
        "vram_mb": 24576
      }
    ]
  },
  "changed": false
}
```

### Windows fallback + WMI-enriched output

Example output from a Windows host where PnP data is enriched with WMI details and generic names are resolved via PCI identifiers when possible:

```yaml
ok: [windows-host] => {
  "ansible_facts": {
    "gpu_count": 1,
    "gpu_detection_errors": [],
    "gpus": [
      {
        "bus_reported_name": "NVIDIA GeForce RTX 4090",
        "detection_method": "windows-pnp",
        "device_description": "Video Controller (VGA Compatible)",
        "driver_detected": true,
        "driver_version": "555.12",
        "hardware_ids": [
          "PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1"
        ],
        "index": 0,
        "name": "NVIDIA GeForce RTX 4090",
        "pci_device_id": "2684",
        "pci_id": "PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1\\4&123",
        "pci_vendor_id": "10DE",
        "problem_code": 0,
        "reported_name": "Microsoft Basic Display Adapter",
        "resolved_name": "NVIDIA GeForce RTX 4090",
        "status": "OK",
        "temperature_c": null,
        "utilization_pct": null,
        "uuid": null,
        "vendor": "nvidia",
        "vram_free_mb": null,
        "vram_mb": 24576
      }
    ]
  },
  "changed": false
}
```

### macOS system_profiler output

Example output from a macOS host using the `system_profiler` fallback path:

```yaml
ok: [macos-host] => {
  "ansible_facts": {
    "gpu_count": 1,
    "gpu_detection_errors": [
      "nvidia-smi unavailable: nvidia-smi not found"
    ],
    "gpus": [
      {
        "detection_method": "system_profiler",
        "driver_detected": false,
        "driver_version": null,
        "index": 0,
        "name": "Apple M2",
        "pci_id": "spdisplays_builtin",
        "temperature_c": null,
        "utilization_pct": null,
        "uuid": null,
        "vendor": "unknown",
        "vram_free_mb": null,
        "vram_mb": 8192
      }
    ]
  },
  "changed": false
}
```

Additional Windows-oriented fields that may appear on each GPU object:

- `pci_vendor_id`
- `pci_device_id`
- `hardware_ids`
- `reported_name`
- `bus_reported_name`
- `device_description`
- `resolved_name`
- `status`
- `problem_code`

## Versioning

- Start at 0.1.0
- Keep schema additive across releases

## License

MIT
