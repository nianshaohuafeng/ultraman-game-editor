"""Identify the original FE3 finisher glyphs, including rows copied into another atlas."""

from functools import lru_cache
from hashlib import blake2b

from fe3_formats import png_for_pom
from fe3_native import FINISHER_NAMES
from kgf_readers.pom_to_tm2 import decode_png


ORIGINAL_ROWS = {
    0: {1: '3aeb39cfa4e543fa', 2: '06ebc323cfb515f3', 3: 'a35864910987b53a', 4: 'e230ecfd8e372a38', 5: 'd0053e4b32611ba9', 6: '81c6fceff45e9171'},
    1: {0: 'b45161201e89ae5f', 1: '27c6b782c8945dbc', 2: 'c559cd1870c0599a', 3: 'aebfeae97dba59ab', 4: '7ea2f16b51fee8b3', 5: 'ccf4e4750adfcbba', 6: 'c8b349b426ae6656'},
    2: {1: '940c5294cc4053cc', 2: '0f92d1d8d6affbb1', 3: '06ebc323cfb515f3', 4: '8df37dad3ff983f2', 5: '1ed6697eb9b5f9e1', 6: 'feabdc2e097ab9c6', 7: '35fbf08526063119', 8: '757cf0d3cbad8bb8', 9: '581a9afaf2fb22c7'},
    3: {1: 'beb50fa08c17848e', 2: 'd71dc01a87055b0f', 3: '3762362618d05b66', 4: 'e18095cea37489d5', 6: 'c72c7f951f010a10', 7: '4aac0877c07639ef', 8: '84d538e6567c227e', 9: 'ee48fb69dde27cc7', 10: '9420b12610a92dd7', 11: 'c4caf757536d414f', 12: '2b69bcf59afa9814'},
    4: {1: '95a68476010f00d8', 2: 'ad99f76ec3c36cf3', 3: '5978b110c913e71f', 4: '99ba9d3a2530c8dc', 5: '453fc1cb90d1d6d8'},
    5: {0: '40a528558d0f7f7a', 1: '44c317687e9b37b6', 2: '78230152cd417149', 3: '0141df6ccbc34978', 4: '2028cca20cda2512', 5: 'd38fe1612f7b9cdc', 6: '1fada2a91f6f7a40'},
    6: {0: 'ad0fa04109de45cc', 2: '5cdd71d7db3deb36'},
    7: {1: '0d1483f8bcf82fdc', 2: '434378093461256e'},
    8: {1: '89af0e3bdd5fcd97'},
    9: {1: '19d7c47d5820aca2'},
    10: {0: '83ef79c8da7c9889', 1: 'f8694c30b4276271'},
    11: {2: 'd382a36c05ae8733'},
    12: {0: 'b510208ed422f5ef', 1: 'ebe2d03fbabff234'},
    13: {0: '39577a64771fccd2', 1: '58098477cb01f969'},
    14: {1: '10cee4c634f2439a'},
    15: {1: 'a15ec494550ffccb'},
    16: {0: 'da0d840c601b0ea5', 1: 'f111d1c33699357c', 2: '6fb4227f2e394aa4', 3: 'ca0a25f1bfc268dc'},
    17: {0: 'fe87a00e69ccddc2', 1: '7a1a489f15323de1', 2: 'af7bca62b7e48f53', 3: '31fb9f1e2f4d6164', 4: '7411778019878cce', 5: 'c4efcdb2cdb1710d', 6: 'e6957ae8abf9794f'},
    18: {0: 'fda05d84215cd5fb', 1: 'cf03db8f9b7ada30', 2: '7393e1959241457e', 3: '8c3e6b1ca8d7a0ec', 4: 'd1407c103cf5afb9'},
    19: {0: '14be1099b39bf993', 1: '7519f4f95b642d69', 2: '582fa9e1c5310275', 3: '003bcf4db9f0390e', 4: '8cbbf7d83df82c75'},
    20: {0: 'a9d1ee15fb541c51', 1: '8d479205e1b86c54', 2: 'f359bf77e8d8c650', 3: 'a8e2283bc19af67c'},
    21: {0: '61043dfce2f9317f', 1: '0d57549de879a381', 2: '30870f0729fe8746', 3: '1cb6a893539a9f84', 4: '02025e205f9654a9'},
    22: {0: '9cb9e3dea1ef5b10', 1: '5d0140aa8d77dd4d', 2: '02025e205f9654a9'},
    24: {0: '2f89951a3295d856'},
    25: {0: '3e07354d93ab154f', 1: '63b49241149c1ffe'},
    26: {0: 'e46ffed3a69b020f'},
    27: {0: '214f8c3203a15afa'},
    28: {0: '364c3a70e8b32e79', 1: 'f9d80213bd9baa6a'},
    29: {0: '2af11f196808e8c5'},
    30: {0: '1182c630f68b82f1'},
    31: {0: 'ed0b141a5ebfc1a8'},
    32: {0: '7dcfda8cb0573c82'},
    33: {0: '68513cbdf065e18e'},
    34: {0: '914ce85bbee45cdd'},
    35: {0: '895528b5261ace24'},
    36: {0: 'b737755c70206a9e', 1: '44c317687e9b37b6'},
    37: {0: 'cd6c08aa3d451e57'},
    38: {0: '61a9cd5e1d21951c'},
}
NAMES_BY_ROW = {fingerprint:FINISHER_NAMES[role][candidate] for role, rows in ORIGINAL_ROWS.items()
                for candidate, fingerprint in rows.items()}


@lru_cache(maxsize=64)
def atlas_rows(data):
    image = decode_png(png_for_pom(data, 0))
    step = image.width * 14 * 4
    return [blake2b(image.rgba[at:at+step], digest_size=8).hexdigest()
            for at in range(0, len(image.rgba)-step+1, step)]


def atlas_name(rows, role, candidate, default):
    if candidate >= len(rows) or rows[candidate] == ORIGINAL_ROWS.get(role, {}).get(candidate):
        return default
    return NAMES_BY_ROW.get(rows[candidate], default)
