"""Static TAG v2 vectors shared by firmware, oglo-sdk, and SyncField.

These bytes are protocol fixtures, not evidence captured from a physical glove.
Final 0.9.13 qualification must add a signed-firmware hardware capture.
"""

from syncfield.adapters.oglo.usb_packet import iter_usb_packets


VECTORS = {
    "tactile": {
        "hex": (
            "a55b017800040302013412000003000000"
            "1f41f51f61f71f81f91fa1fb1fc1fd1fe1ff200201202203204205206207208"
            "20920a20b20c20d20e20f21021121221321421521621721821921a21b21c21d"
            "21e21f22022122222322422522622722822922a22b22c22d22e22f230231232"
            "23323423523623723823923a23b23c23d23e23f240241242243"
        ),
        "seq": 0x01020304,
        "timestamp_us": (3 << 32) + 0x1234,
        "values": tuple(range(500, 580)),
    },
    "imu": {
        "hex": "a55b020c00ddccbbaa63000000040000000010000000f0a40000005cff",
        "seq": 0xAABBCCDD,
        "timestamp_us": (4 << 32) + 99,
        "values": (4096, 0, -4096, 164, 0, -164),
    },
    "mag": {
        "hex": "a55b030600070000007b00000005000000ba1a0000a3f2",
        "seq": 7,
        "timestamp_us": (5 << 32) + 123,
        "values": (6842, 0, -3421),
    },
}


def test_locked_tag_v2_vectors_decode_exactly() -> None:
    for name, vector in VECTORS.items():
        packets, remainder = iter_usb_packets(bytes.fromhex(vector["hex"]))
        packet_list = list(packets)
        assert remainder == b""
        assert len(packet_list) == 1
        packet = packet_list[0]
        assert packet.tag_version == 2
        assert packet.modality == name
        assert packet.seq == vector["seq"]
        assert packet.device_us == vector["timestamp_us"]
        assert packet.values == vector["values"]
