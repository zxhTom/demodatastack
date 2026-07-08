"""pgoutput 逻辑复制协议解析（proto_version=1）。

维护 relation_id -> 表结构映射（来自 R 消息），把 I/U/D 消息还原成
带真实列名的 before/after 字典，值为文本格式字符串。
"""
import struct

# UPDATE 中未变化的 TOAST 大字段，pgoutput 不会重发内容
UNCHANGED_TOAST = "__unchanged_toast__"


def _cstring(buf: bytes, pos: int):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8"), end + 1


def _tuple_data(buf: bytes, pos: int):
    (ncols,) = struct.unpack_from(">H", buf, pos)
    pos += 2
    values = []
    for _ in range(ncols):
        kind = buf[pos:pos + 1]
        pos += 1
        if kind == b"n":
            values.append(None)
        elif kind == b"u":
            values.append(UNCHANGED_TOAST)
        else:  # 't' 文本格式
            (length,) = struct.unpack_from(">I", buf, pos)
            pos += 4
            values.append(buf[pos:pos + length].decode("utf-8"))
            pos += length
    return values, pos


class PgOutputParser:
    def __init__(self):
        self.relations = {}  # rel_id -> (schema, table, [列名])

    def parse(self, payload: bytes):
        """解析一条 WAL 消息。返回变更事件 dict，控制消息（B/C/R/...）返回 None。"""
        if not payload:
            return None
        tag = payload[:1]
        if tag == b"R":
            self._parse_relation(payload)
            return None
        if tag not in (b"I", b"U", b"D"):
            return None

        (rel_id,) = struct.unpack_from(">I", payload, 1)
        rel = self.relations.get(rel_id)
        if rel is None:
            return None
        schema, table, cols = rel

        pos = 5
        before = after = None
        while pos < len(payload):
            sub = payload[pos:pos + 1]
            pos += 1
            values, pos = _tuple_data(payload, pos)
            row = dict(zip(cols, values))
            if sub in (b"K", b"O"):
                before = row
            elif sub == b"N":
                after = row
            else:
                break

        op = {b"I": "c", b"U": "u", b"D": "d"}[tag]
        return {"op": op, "schema": schema, "table": table,
                "before": before, "after": after}

    def _parse_relation(self, payload: bytes):
        pos = 1
        (rel_id,) = struct.unpack_from(">I", payload, pos)
        pos += 4
        schema, pos = _cstring(payload, pos)
        table, pos = _cstring(payload, pos)
        pos += 1  # replica identity 标志
        (ncols,) = struct.unpack_from(">H", payload, pos)
        pos += 2
        cols = []
        for _ in range(ncols):
            pos += 1  # 列标志位
            name, pos = _cstring(payload, pos)
            pos += 8  # 类型 OID + atttypmod
            cols.append(name)
        self.relations[rel_id] = (schema, table, cols)
