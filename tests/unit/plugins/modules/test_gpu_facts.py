from __future__ import absolute_import, division, print_function

__metaclass__ = type

import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Allow importing the module on Windows hosts where ansible internals may rely
# on Linux-only modules such as grp.
basic_mod = types.ModuleType('ansible.module_utils.basic')


class _DummyAnsibleModule:
    def __init__(self, *args, **kwargs):
        pass


basic_mod.AnsibleModule = _DummyAnsibleModule
sys.modules['ansible.module_utils.basic'] = basic_mod

from plugins.modules import gpu_facts


class DummyModule:
    def __init__(self, responses):
        self.responses = responses

    def get_bin_path(self, command, required=False):
        for key in self.responses:
            if key and key[0] == command:
                return command
        return None

    def run_command(self, cmd, check_rc=False):
        key = tuple(cmd)
        if key in self.responses:
            return self.responses[key]
        return 1, '', 'missing command mock'


def test_safe_int():
    assert gpu_facts._safe_int('10') == 10
    assert gpu_facts._safe_int('10.5') == 10
    assert gpu_facts._safe_int(None) is None


def test_vendor_from_name():
    assert gpu_facts._vendor_from_name('NVIDIA RTX 4090') == 'nvidia'
    assert gpu_facts._vendor_from_name('AMD Radeon 7900') == 'amd'
    assert gpu_facts._vendor_from_name('Intel Arc A770') == 'intel'


def test_parse_vram_mb():
    assert gpu_facts._parse_vram_mb('16 GB') == 16384
    assert gpu_facts._parse_vram_mb('512 MB') == 512
    assert gpu_facts._parse_vram_mb('unknown') is None


def test_scan_macos_system_profiler_parses_displays_data():
    errors = []
    payload = __import__('json').dumps(
        {
            'SPDisplaysDataType': [
                {
                    'sppci_model': 'Apple M2',
                    'sppci_vram': '8 GB',
                    'sppci_bus': 'spdisplays_builtin',
                }
            ]
        }
    )
    module = DummyModule({
        ('system_profiler', 'SPDisplaysDataType', '-json'): (0, payload, ''),
    })

    gpus = gpu_facts._scan_macos_system_profiler(module, errors)

    assert len(gpus) == 1
    assert gpus[0]['name'] == 'Apple M2'
    assert gpus[0]['detection_method'] == 'system_profiler'
    assert gpus[0]['vram_mb'] == 8192
    assert gpus[0]['pci_id'] == 'spdisplays_builtin'
    assert errors == []


def test_detect_nvidia_smi_parses_csv():
    errors = []
    line = '0, NVIDIA GeForce RTX 4090, 555.12, 24576, 12000, 40, 10, 00000000:01:00.0, GPU-1234'
    module = DummyModule({
        (
            'nvidia-smi',
            '--query-gpu=index,name,driver_version,memory.total,memory.free,temperature.gpu,utilization.gpu,pci.bus_id,uuid',
            '--format=csv,noheader,nounits',
        ): (0, line, ''),
    })

    gpus = gpu_facts._detect_nvidia_smi(module, errors)

    assert len(gpus) == 1
    assert gpus[0]['vendor'] == 'nvidia'
    assert gpus[0]['driver_detected'] is True
    assert gpus[0]['vram_mb'] == 24576
    assert gpus[0]['pci_id'] == '01:00.0'
    assert errors == []


def test_merge_gpus_deduplicates_by_name():
    primary = [{'name': 'NVIDIA A100', 'index': 0}]
    fallback = [{'name': 'NVIDIA A100', 'index': 0}, {'name': 'Intel Arc', 'index': 1}]

    merged = gpu_facts._merge_gpus(primary, fallback)

    assert len(merged) == 2
    assert merged[1]['name'] == 'Intel Arc'


def test_run_returns_missing_binary_error_without_raising():
    module = DummyModule({})

    rc, out, err = gpu_facts._run(module, ['nvidia-smi', '--help'])

    assert rc == 1
    assert out == ''
    assert err == 'nvidia-smi not found'


def test_extract_pci_ids_from_hardware_id_text():
    vendor_id, device_id = gpu_facts._extract_pci_ids('PCI\\VEN_10DE&DEV_2684&SUBSYS_00000000')

    assert vendor_id == '10DE'
    assert device_id == '2684'


def test_vendor_from_pci_vendor_id():
    assert gpu_facts._vendor_from_pci_vendor_id('10de') == 'nvidia'
    assert gpu_facts._vendor_from_pci_vendor_id('1002') == 'amd'
    assert gpu_facts._vendor_from_pci_vendor_id('8086') == 'intel'
    assert gpu_facts._vendor_from_pci_vendor_id('0000') == 'unknown'


def test_scan_windows_merges_pnp_with_wmi_enrichment():
    pnp_records = [
        {
            'Name': 'Microsoft Basic Display Adapter',
            'InstanceId': 'PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1\\4&123',
            'HardwareIds': ['PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1'],
            'Status': 'OK',
            'ProblemCode': 0,
        }
    ]
    wmi_records = [
        {
            'Name': 'NVIDIA GeForce RTX 4090',
            'AdapterRAM': 25769803776,
            'DriverVersion': '555.12',
            'PNPDeviceID': 'PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1\\4&123',
        }
    ]

    original_run = gpu_facts._run

    def fake_run(module, cmd):
        if cmd[:3] == ['powershell', '-NonInteractive', '-Command']:
            script = cmd[3]
            if 'Get-PnpDevice -Class Display' in script:
                return 0, __import__('json').dumps(pnp_records), ''
            if 'Get-CimInstance Win32_VideoController' in script:
                return 0, __import__('json').dumps(wmi_records), ''
        return 1, '', 'unexpected command'

    try:
        gpu_facts._run = fake_run
        errors = []
        gpus = gpu_facts._scan_windows(object(), errors)
    finally:
        gpu_facts._run = original_run

    assert errors == []
    assert len(gpus) == 1
    assert gpus[0]['vendor'] == 'nvidia'
    assert gpus[0]['pci_vendor_id'] == '10DE'
    assert gpus[0]['pci_device_id'] == '2684'
    assert gpus[0]['driver_detected'] is True
    assert gpus[0]['driver_version'] == '555.12'
    assert gpus[0]['vram_mb'] == 24576


def test_resolve_name_from_pci_ids_repo_lookup():
    original_get_lookup = gpu_facts._get_pci_lookup

    try:
        gpu_facts._get_pci_lookup = lambda: {'10DE': {'2684': 'NVIDIA GeForce RTX 4090'}}
        assert gpu_facts._resolve_name_from_pci_ids('10DE', '2684') == 'NVIDIA GeForce RTX 4090'
        assert gpu_facts._resolve_name_from_pci_ids('1002', '73BF') is None
    finally:
        gpu_facts._get_pci_lookup = original_get_lookup


def test_resolve_name_from_pci_ids_repo_lookup_multi_vendor():
    original_get_lookup = gpu_facts._get_pci_lookup

    try:
        gpu_facts._get_pci_lookup = lambda: {
            '1002': {'744C': 'AMD Radeon RX 7900 XTX'},
            '8086': {'56A0': 'Intel Arc A770'},
        }
        assert gpu_facts._resolve_name_from_pci_ids('1002', '744C') == 'AMD Radeon RX 7900 XTX'
        assert gpu_facts._resolve_name_from_pci_ids('8086', '56A0') == 'Intel Arc A770'
    finally:
        gpu_facts._get_pci_lookup = original_get_lookup


def test_parse_pci_ids_lookup_file_parses_vendor_and_devices():
    sample = "10de  NVIDIA Corporation\n\t2684  AD102 [GeForce RTX 4090]\n1002  AMD\n\t744c  Navi 31 [Radeon RX 7900 XTX]\n"
    with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8') as tmp:
        tmp.write(sample)
        path = tmp.name

    try:
        lookup = gpu_facts._parse_pci_ids_lookup_file(path)
    finally:
        Path(path).unlink(missing_ok=True)

    assert lookup['10DE']['2684'] == 'NVIDIA Corporation AD102 [GeForce RTX 4090]'
    assert lookup['1002']['744C'] == 'AMD Navi 31 [Radeon RX 7900 XTX]'


def test_get_pci_lookup_merges_linux_packaged_and_repo_sources():
    original_cache = gpu_facts._PCI_LOOKUP_CACHE
    original_linux = gpu_facts._load_linux_pci_ids_lookup
    original_packaged = gpu_facts._load_packaged_pci_ids_lookup
    original_repo = gpu_facts._load_repo_pci_lookup

    try:
        gpu_facts._PCI_LOOKUP_CACHE = None
        gpu_facts._load_linux_pci_ids_lookup = lambda: {'10DE': {'2684': 'Linux Name'}}
        gpu_facts._load_packaged_pci_ids_lookup = lambda: {'10DE': {'2204': 'Packaged Name'}}
        gpu_facts._load_repo_pci_lookup = lambda: {'10DE': {'2782': 'Repo Name'}}

        lookup = gpu_facts._get_pci_lookup()
    finally:
        gpu_facts._PCI_LOOKUP_CACHE = original_cache
        gpu_facts._load_linux_pci_ids_lookup = original_linux
        gpu_facts._load_packaged_pci_ids_lookup = original_packaged
        gpu_facts._load_repo_pci_lookup = original_repo

    assert lookup['10DE']['2684'] == 'Linux Name'
    assert lookup['10DE']['2204'] == 'Packaged Name'
    assert lookup['10DE']['2782'] == 'Repo Name'


def test_scan_windows_uses_lookup_when_name_is_generic():
    pnp_records = [
        {
            'Name': 'Microsoft Basic Display Adapter',
            'InstanceId': 'PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1\\4&123',
            'HardwareIds': ['PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1'],
            'BusReportedDeviceDesc': None,
            'DeviceDesc': 'Video Controller (VGA Compatible)',
            'Status': 'OK',
            'ProblemCode': 0,
        }
    ]
    wmi_records = [
        {
            'Name': 'Microsoft Basic Display Adapter',
            'AdapterRAM': None,
            'DriverVersion': None,
            'PNPDeviceID': 'PCI\\VEN_10DE&DEV_2684&SUBSYS_12345678&REV_A1\\4&123',
        }
    ]

    original_run = gpu_facts._run
    original_get_lookup = gpu_facts._get_pci_lookup

    def fake_run(module, cmd):
        if cmd[:3] == ['powershell', '-NonInteractive', '-Command']:
            script = cmd[3]
            if 'Get-PnpDevice -Class Display' in script:
                return 0, __import__('json').dumps(pnp_records), ''
            if 'Get-CimInstance Win32_VideoController' in script:
                return 0, __import__('json').dumps(wmi_records), ''
        return 1, '', 'unexpected command'

    try:
        gpu_facts._run = fake_run
        gpu_facts._get_pci_lookup = lambda: {'10DE': {'2684': 'NVIDIA GeForce RTX 4090'}}
        errors = []
        gpus = gpu_facts._scan_windows(object(), errors)
    finally:
        gpu_facts._run = original_run
        gpu_facts._get_pci_lookup = original_get_lookup

    assert errors == []
    assert len(gpus) == 1
    assert gpus[0]['name'] == 'NVIDIA GeForce RTX 4090'
    assert gpus[0]['resolved_name'] == 'NVIDIA GeForce RTX 4090'


def test_scan_windows_uses_lookup_when_name_is_generic_amd():
    pnp_records = [
        {
            'Name': 'Microsoft Basic Display Adapter',
            'InstanceId': 'PCI\\VEN_1002&DEV_744C&SUBSYS_12345678&REV_C8\\4&123',
            'HardwareIds': ['PCI\\VEN_1002&DEV_744C&SUBSYS_12345678&REV_C8'],
            'BusReportedDeviceDesc': None,
            'DeviceDesc': 'Display Adapter',
            'Status': 'OK',
            'ProblemCode': 0,
        }
    ]
    wmi_records = [
        {
            'Name': 'Microsoft Basic Display Adapter',
            'AdapterRAM': None,
            'DriverVersion': None,
            'PNPDeviceID': 'PCI\\VEN_1002&DEV_744C&SUBSYS_12345678&REV_C8\\4&123',
        }
    ]

    original_run = gpu_facts._run
    original_get_lookup = gpu_facts._get_pci_lookup

    def fake_run(module, cmd):
        if cmd[:3] == ['powershell', '-NonInteractive', '-Command']:
            script = cmd[3]
            if 'Get-PnpDevice -Class Display' in script:
                return 0, __import__('json').dumps(pnp_records), ''
            if 'Get-CimInstance Win32_VideoController' in script:
                return 0, __import__('json').dumps(wmi_records), ''
        return 1, '', 'unexpected command'

    try:
        gpu_facts._run = fake_run
        gpu_facts._get_pci_lookup = lambda: {'1002': {'744C': 'AMD Radeon RX 7900 XTX'}}
        errors = []
        gpus = gpu_facts._scan_windows(object(), errors)
    finally:
        gpu_facts._run = original_run
        gpu_facts._get_pci_lookup = original_get_lookup

    assert errors == []
    assert len(gpus) == 1
    assert gpus[0]['vendor'] == 'amd'
    assert gpus[0]['name'] == 'AMD Radeon RX 7900 XTX'
    assert gpus[0]['resolved_name'] == 'AMD Radeon RX 7900 XTX'
