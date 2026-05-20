from __future__ import annotations

from random import randint
from typing import Iterable, Iterator

from .openbridge import int_to_3, int_to_4

HEADBITS = 0b00100001
BURSTBITS = [0b00010000, 0b00000001, 0b00000010, 0b00000011, 0b00000100, 0b00000101]
TERMBITS = 0b00100010


def _imports():
    try:
        from bitarray import bitarray
        from dmr_utils3 import bptc
        from dmr_utils3.const import EMB, SLOT_TYPE, BS_VOICE_SYNC, BS_DATA_SYNC, LC_OPT
    except ImportError as exc:
        raise RuntimeError(
            "direct_openbridge requires optional dependencies: pip install 'weather-alert-system[direct-openbridge]' "
            "or install bitarray and dmr-utils3."
        ) from exc
    return bitarray, bptc, EMB, SLOT_TYPE, BS_VOICE_SYNC, BS_DATA_SYNC, LC_OPT


def group_ambe72_frames(frames: Iterable[bytes], silence_frame: bytes) -> Iterator[bytes]:
    """Group 9-byte AMBE72 frames into 27-byte DMR voice bursts."""
    bucket: list[bytes] = []
    for frame in frames:
        if len(frame) != 9:
            raise ValueError(f"AMBE72 frame must be 9 bytes, got {len(frame)}")
        bucket.append(frame)
        if len(bucket) == 3:
            yield b"".join(bucket)
            bucket = []
    if bucket:
        while len(bucket) < 3:
            bucket.append(silence_frame)
        yield b"".join(bucket)


def ambe_burst_to_halves(burst: bytes):
    bitarray, *_ = _imports()
    if len(burst) != 27:
        raise ValueError(f"DMR AMBE burst must be 27 bytes, got {len(burst)}")
    bits = bitarray(endian="big")
    bits.frombytes(burst)
    if len(bits) != 216:
        raise ValueError("Internal AMBE burst bit length error")
    return bits[:108], bits[108:216]


def dmrd_packets_from_ambe72(
    frames: Iterable[bytes],
    rf_src_id: int,
    dst_talkgroup: int,
    peer_id: int,
    slot: int,
    silence_frame: bytes,
) -> Iterator[bytes]:
    bitarray, bptc, EMB, SLOT_TYPE, BS_VOICE_SYNC, BS_DATA_SYNC, LC_OPT = _imports()

    if slot not in (1, 2):
        raise ValueError("slot must be 1 or 2")
    slot_flag = 0 if slot == 1 else 1
    stream_id = int_to_4(randint(0x00, 0xFFFFFFFF))
    rf_src = int_to_3(rf_src_id)
    dst_id = int_to_3(dst_talkgroup)
    peer = int_to_4(peer_id)
    sdp = rf_src + dst_id + peer
    lc = LC_OPT + dst_id + rf_src

    head_lc = bptc.encode_header_lc(lc)
    head_lc = [head_lc[:98], head_lc[-98:]]
    term_lc = bptc.encode_terminator_lc(lc)
    term_lc = [term_lc[:98], term_lc[-98:]]
    emb_lc = bptc.encode_emblc(lc)

    null_emb_lc = bitarray(endian="big")
    null_emb_lc.frombytes(b"\x00\x00\x00\x00")
    embed = [
        BS_VOICE_SYNC,
        EMB["BURST_B"][:8] + emb_lc[1] + EMB["BURST_B"][-8:],
        EMB["BURST_C"][:8] + emb_lc[2] + EMB["BURST_C"][-8:],
        EMB["BURST_D"][:8] + emb_lc[3] + EMB["BURST_D"][-8:],
        EMB["BURST_E"][:8] + emb_lc[4] + EMB["BURST_E"][-8:],
        EMB["BURST_F"][:8] + null_emb_lc + EMB["BURST_F"][-8:],
    ]

    seq = 0
    for _ in range(3):
        payload = (head_lc[0] + SLOT_TYPE["VOICE_LC_HEAD"][:10] + BS_DATA_SYNC + SLOT_TYPE["VOICE_LC_HEAD"][-10:] + head_lc[1]).tobytes()
        yield b"DMRD" + bytes([seq]) + sdp + bytes([(slot_flag << 7) | HEADBITS]) + stream_id + payload
        seq = (seq + 1) % 0x100

    burst_index = 0
    for burst in group_ambe72_frames(frames, silence_frame):
        first, second = ambe_burst_to_halves(burst)
        payload = (first + embed[burst_index % 6] + second).tobytes()
        yield b"DMRD" + bytes([seq]) + sdp + bytes([(slot_flag << 7) | BURSTBITS[burst_index % 6]]) + stream_id + payload
        seq = (seq + 1) % 0x100
        burst_index += 1

    payload = (term_lc[0] + SLOT_TYPE["VOICE_LC_TERM"][:10] + BS_DATA_SYNC + SLOT_TYPE["VOICE_LC_TERM"][-10:] + term_lc[1]).tobytes()
    yield b"DMRD" + bytes([seq]) + sdp + bytes([(slot_flag << 7) | TERMBITS]) + stream_id + payload
