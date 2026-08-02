"""Small dependency-free QR encoder for G4W login URLs.

The implementation intentionally supports byte mode with error-correction level L,
which is the format used by the Weixin bot login endpoint. Versions 1-10 cover
the current login URL with comfortable headroom.
"""

from __future__ import annotations


_RS_BLOCKS_L = {
    1: [(1, 26, 19)],
    2: [(1, 44, 34)],
    3: [(1, 70, 55)],
    4: [(1, 100, 80)],
    5: [(1, 134, 108)],
    6: [(2, 86, 68)],
    7: [(2, 98, 78)],
    8: [(2, 121, 97)],
    9: [(2, 146, 116)],
    10: [(2, 86, 68), (2, 87, 69)],
}

_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
    9: [6, 26, 46], 10: [6, 28, 50],
}


class _Bits:
    def __init__(self):
        self.values: list[int] = []

    def put(self, value: int, length: int) -> None:
        self.values.extend((value >> (length - index - 1)) & 1 for index in range(length))


def _gf_mul(left: int, right: int) -> int:
    value = 0
    while right:
        if right & 1:
            value ^= left
        right >>= 1
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
    return value


def _generator(degree: int) -> list[int]:
    result = [1]
    root = 1
    for _ in range(degree):
        next_result = [0] * (len(result) + 1)
        for index, coefficient in enumerate(result):
            next_result[index] ^= coefficient
            next_result[index + 1] ^= _gf_mul(coefficient, root)
        result = next_result
        root = _gf_mul(root, 2)
    return result


def _ecc(data: list[int], degree: int) -> list[int]:
    divisor = _generator(degree)
    message = data + [0] * degree
    for index in range(len(data)):
        factor = message[index]
        if factor:
            for offset, coefficient in enumerate(divisor):
                message[index + offset] ^= _gf_mul(coefficient, factor)
    return message[-degree:]


def _blocks(version: int) -> list[tuple[int, int]]:
    return [(total, data) for count, total, data in _RS_BLOCKS_L[version] for _ in range(count)]


def _codewords(content: str, version: int) -> list[int]:
    payload = content.encode("utf-8")
    blocks = _blocks(version)
    data_capacity = sum(data for _, data in blocks)
    bits = _Bits()
    bits.put(0b0100, 4)
    bits.put(len(payload), 8 if version < 10 else 16)
    for value in payload:
        bits.put(value, 8)
    capacity_bits = data_capacity * 8
    if len(bits.values) > capacity_bits:
        raise ValueError("content does not fit QR version")
    bits.values.extend([0] * min(4, capacity_bits - len(bits.values)))
    bits.values.extend([0] * ((8 - len(bits.values) % 8) % 8))
    data_bytes = [sum(bit << (7 - offset) for offset, bit in enumerate(bits.values[index:index + 8])) for index in range(0, len(bits.values), 8)]
    pads = (0xEC, 0x11)
    while len(data_bytes) < data_capacity:
        data_bytes.append(pads[(len(data_bytes) - len(bits.values) // 8) % 2])

    data_blocks: list[list[int]] = []
    ecc_blocks: list[list[int]] = []
    cursor = 0
    for total_count, data_count in blocks:
        block = data_bytes[cursor:cursor + data_count]
        cursor += data_count
        data_blocks.append(block)
        ecc_blocks.append(_ecc(block, total_count - data_count))
    output = []
    for index in range(max(map(len, data_blocks))):
        output.extend(block[index] for block in data_blocks if index < len(block))
    for index in range(max(map(len, ecc_blocks))):
        output.extend(block[index] for block in ecc_blocks if index < len(block))
    return output


def _bch_type_info(value: int) -> int:
    data = value << 10
    generator = 0x537
    while data.bit_length() >= generator.bit_length():
        data ^= generator << (data.bit_length() - generator.bit_length())
    return ((value << 10) | data) ^ 0x5412


def _bch_type_number(value: int) -> int:
    data = value << 12
    generator = 0x1F25
    while data.bit_length() >= generator.bit_length():
        data ^= generator << (data.bit_length() - generator.bit_length())
    return (value << 12) | data


def _mask(mask: int, row: int, col: int) -> bool:
    return (
        (row + col) % 2 == 0,
        row % 2 == 0,
        col % 3 == 0,
        (row + col) % 3 == 0,
        (row // 2 + col // 3) % 2 == 0,
        row * col % 2 + row * col % 3 == 0,
        (row * col % 2 + row * col % 3) % 2 == 0,
        ((row * col) % 3 + (row + col) % 2) % 2 == 0,
    )[mask]


def _finder(matrix, row: int, col: int) -> None:
    size = len(matrix)
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = row + dr, col + dc
            if 0 <= rr < size and 0 <= cc < size:
                matrix[rr][cc] = (
                    (0 <= dr <= 6 and dc in (0, 6))
                    or (0 <= dc <= 6 and dr in (0, 6))
                    or (2 <= dr <= 4 and 2 <= dc <= 4)
                )


def _base_matrix(version: int, mask: int, test: bool = False) -> list[list[bool | None]]:
    size = version * 4 + 17
    matrix: list[list[bool | None]] = [[None] * size for _ in range(size)]
    _finder(matrix, 0, 0)
    _finder(matrix, size - 7, 0)
    _finder(matrix, 0, size - 7)

    positions = _ALIGNMENT[version]
    for row in positions:
        for col in positions:
            if matrix[row][col] is not None:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    matrix[row + dr][col + dc] = max(abs(dr), abs(dc)) != 1

    for index in range(8, size - 8):
        if matrix[index][6] is None:
            matrix[index][6] = index % 2 == 0
        if matrix[6][index] is None:
            matrix[6][index] = index % 2 == 0

    type_info = _bch_type_info((1 << 3) | mask)  # L level is value 1.
    for index in range(15):
        bit = False if test else bool((type_info >> index) & 1)
        if index < 6:
            matrix[index][8] = bit
        elif index < 8:
            matrix[index + 1][8] = bit
        else:
            matrix[size - 15 + index][8] = bit
        if index < 8:
            matrix[8][size - index - 1] = bit
        elif index < 9:
            matrix[8][15 - index] = bit
        else:
            matrix[8][15 - index - 1] = bit
    matrix[size - 8][8] = not test

    if version >= 7:
        type_number = _bch_type_number(version)
        for index in range(18):
            bit = False if test else bool((type_number >> index) & 1)
            matrix[index // 3][index % 3 + size - 11] = bit
            matrix[index % 3 + size - 11][index // 3] = bit
    return matrix


def _place_data(matrix, codewords: list[int], mask: int) -> None:
    bits = [(value >> shift) & 1 for value in codewords for shift in range(7, -1, -1)]
    size = len(matrix)
    bit_index = 0
    row = size - 1
    direction = -1
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        while True:
            for offset in range(2):
                target_col = col - offset
                if matrix[row][target_col] is None:
                    bit = bool(bits[bit_index]) if bit_index < len(bits) else False
                    bit_index += 1
                    matrix[row][target_col] = bit ^ _mask(mask, row, target_col)
            row += direction
            if row < 0 or row >= size:
                row -= direction
                direction = -direction
                break
        col -= 2


def _penalty(matrix: list[list[bool]]) -> int:
    size = len(matrix)
    score = 0
    for row in range(size):
        for col in range(size):
            value = matrix[row][col]
            same = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == dc == 0 or not (0 <= row + dr < size and 0 <= col + dc < size):
                        continue
                    same += matrix[row + dr][col + dc] == value
            if same > 5:
                score += 3 + same - 5
    for row in range(size - 1):
        for col in range(size - 1):
            count = sum((matrix[row][col], matrix[row + 1][col], matrix[row][col + 1], matrix[row + 1][col + 1]))
            if count in (0, 4):
                score += 3
    pattern = (True, False, True, True, True, False, True)
    for index in range(size):
        row_values = tuple(matrix[index])
        col_values = tuple(matrix[row][index] for row in range(size))
        for offset in range(size - 6):
            if row_values[offset:offset + 7] == pattern:
                score += 40
            if col_values[offset:offset + 7] == pattern:
                score += 40
    dark = sum(sum(row) for row in matrix)
    score += abs(100 * dark / (size * size) - 50) // 5 * 10
    return int(score)


def make_matrix(content: str) -> list[list[bool]]:
    payload_length = len(content.encode("utf-8"))
    version = next((candidate for candidate in _RS_BLOCKS_L if payload_length + (2 if candidate < 10 else 3) <= sum(data for _, data in _blocks(candidate))), None)
    if version is None:
        raise ValueError("QR content is too long (maximum supported version is 10-L)")
    codewords = _codewords(content, version)
    candidates = []
    for mask in range(8):
        matrix = _base_matrix(version, mask, test=True)
        _place_data(matrix, codewords, mask)
        completed = [[bool(value) for value in row] for row in matrix]
        candidates.append((_penalty(completed), mask))
    chosen_mask = min(candidates, key=lambda item: item[0])[1]
    matrix = _base_matrix(version, chosen_mask)
    _place_data(matrix, codewords, chosen_mask)
    return [[bool(value) for value in row] for row in matrix]


def make_svg(content: str, cell_size: int = 10, quiet_zone: int = 4) -> str:
    matrix = make_matrix(content)
    size = (len(matrix) + quiet_zone * 2) * cell_size
    rects = []
    for row, values in enumerate(matrix):
        for col, dark in enumerate(values):
            if dark:
                rects.append(
                    f'<rect x="{(col + quiet_zone) * cell_size}" y="{(row + quiet_zone) * cell_size}" '
                    f'width="{cell_size}" height="{cell_size}"/>'
                )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        'width="420" height="420" role="img" aria-label="WeChat login QR code">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )
