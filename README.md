# granzer04.gpu_facts

An Ansible collection focused on OS-agnostic GPU detection facts.

## Goals

- Detect NVIDIA, AMD, and Intel GPUs.
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

This approach allows detection even when the host only reports generic names such as `Microsoft Basic Display Adapter`.

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

Example output from a host where vendor driver tools are not installed and the module falls back to a hardware scan:

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
        "name": "NVIDIA Corporation TU104 [GeForce RTX 2080 SUPER]",
        "pci_id": "01:00.0",
        "temperature_c": null,
        "utilization_pct": null,
        "uuid": null,
        "vendor": "nvidia",
        "vram_free_mb": null,
        "vram_mb": null
      }
    ]
  },
  "changed": false
}
```

Example output when a vendor driver tool is available:

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

Additional Windows-oriented fields that may appear on each GPU object:

- `pci_vendor_id`
- `pci_device_id`
- `hardware_ids`
- `reported_name`
- `status`
- `problem_code`

## Versioning

- Start at 0.1.0
- Keep schema additive across releases

## License

MIT
