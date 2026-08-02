def _rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _xtime(value: int) -> int:
    return ((value << 1) ^ (0x1B if value & 0x80 else 0)) & 0xFF


def _mul(left: int, right: int) -> int:
    result = 0
    value = left
    factor = right
    while factor:
        if factor & 1:
            result ^= value
        value = _xtime(value)
        factor >>= 1
    return result


def _pow(value: int, exponent: int) -> int:
    result = 1
    base = value
    while exponent:
        if exponent & 1:
            result = _mul(result, base)
        base = _mul(base, base)
        exponent >>= 1
    return result


def _make_sboxes():
    sbox = []
    inverse = [0] * 256
    for value in range(256):
        inv = 0 if value == 0 else _pow(value, 254)
        transformed = inv ^ _rotl8(inv, 1) ^ _rotl8(inv, 2) ^ _rotl8(inv, 3) ^ _rotl8(inv, 4) ^ 0x63
        sbox.append(transformed)
        inverse[transformed] = value
    return tuple(sbox), tuple(inverse)


SBOX, INV_SBOX = _make_sboxes()


def _matrix(block: bytes) -> list[list[int]]:
    return [list(block[index:index + 4]) for index in range(0, len(block), 4)]


def _bytes(matrix: list[list[int]]) -> bytes:
    return bytes(value for column in matrix for value in column)


def _add_round_key(state, key):
    for column in range(4):
        for row in range(4):
            state[column][row] ^= key[column][row]


def _sub_bytes(state, box):
    for column in range(4):
        for row in range(4):
            state[column][row] = box[state[column][row]]


def _shift_rows(state):
    state[0][1], state[1][1], state[2][1], state[3][1] = state[1][1], state[2][1], state[3][1], state[0][1]
    state[0][2], state[1][2], state[2][2], state[3][2] = state[2][2], state[3][2], state[0][2], state[1][2]
    state[0][3], state[1][3], state[2][3], state[3][3] = state[3][3], state[0][3], state[1][3], state[2][3]


def _inv_shift_rows(state):
    state[0][1], state[1][1], state[2][1], state[3][1] = state[3][1], state[0][1], state[1][1], state[2][1]
    state[0][2], state[1][2], state[2][2], state[3][2] = state[2][2], state[3][2], state[0][2], state[1][2]
    state[0][3], state[1][3], state[2][3], state[3][3] = state[1][3], state[2][3], state[3][3], state[0][3]


def _mix_column(column):
    a0, a1, a2, a3 = column
    column[0] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
    column[1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
    column[2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
    column[3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)


def _inv_mix_column(column):
    a0, a1, a2, a3 = column
    column[0] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
    column[1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
    column[2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
    column[3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 key must contain 16 bytes")
    columns = _matrix(key)
    rcon = 1
    while len(columns) < 44:
        word = list(columns[-1])
        if len(columns) % 4 == 0:
            word.append(word.pop(0))
            word = [SBOX[value] for value in word]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        word = [value ^ prior for value, prior in zip(word, columns[-4])]
        columns.append(word)
    return columns


def encrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 16:
        raise ValueError("AES block must contain 16 bytes")
    round_keys = _expand_key(key)
    state = _matrix(block)
    _add_round_key(state, round_keys[:4])
    for round_index in range(1, 10):
        _sub_bytes(state, SBOX)
        _shift_rows(state)
        for column in state:
            _mix_column(column)
        _add_round_key(state, round_keys[round_index * 4:(round_index + 1) * 4])
    _sub_bytes(state, SBOX)
    _shift_rows(state)
    _add_round_key(state, round_keys[40:44])
    return _bytes(state)


def decrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 16:
        raise ValueError("AES block must contain 16 bytes")
    round_keys = _expand_key(key)
    state = _matrix(block)
    _add_round_key(state, round_keys[40:44])
    _inv_shift_rows(state)
    _sub_bytes(state, INV_SBOX)
    for round_index in range(9, 0, -1):
        _add_round_key(state, round_keys[round_index * 4:(round_index + 1) * 4])
        for column in state:
            _inv_mix_column(column)
        _inv_shift_rows(state)
        _sub_bytes(state, INV_SBOX)
    _add_round_key(state, round_keys[:4])
    return _bytes(state)


def encrypt_ecb_pkcs7(data: bytes, key: bytes) -> bytes:
    padding = 16 - (len(data) % 16)
    padded = data + bytes([padding]) * padding
    return b"".join(encrypt_block(padded[index:index + 16], key) for index in range(0, len(padded), 16))


def decrypt_ecb_pkcs7(data: bytes, key: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("AES ciphertext length must be a positive multiple of 16")
    plain = b"".join(decrypt_block(data[index:index + 16], key) for index in range(0, len(data), 16))
    padding = plain[-1]
    if padding < 1 or padding > 16 or plain[-padding:] != bytes([padding]) * padding:
        raise ValueError("invalid PKCS#7 padding")
    return plain[:-padding]
