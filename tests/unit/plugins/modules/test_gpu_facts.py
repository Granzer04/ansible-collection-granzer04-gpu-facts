from __future__ import absolute_import, division, print_function

__metaclass__ = type

import sys
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
