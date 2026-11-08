comptime Q: UInt8 = 34
comptime COMMA: UInt8 = 44
comptime LF: UInt8 = 10
comptime CR: UInt8 = 13

from std.sys import argv
from std.pathlib import Path


def slice_str(buf: Span[UInt8, _], s: Int, n: Int) -> String:
    if n <= 0:
        return String("")
    return String(StringSlice(unsafe_from_utf8=buf[s : s + n]))


def append_str(mut out: List[UInt8], s: StringSlice) -> None:
    out.extend(s.as_bytes())


def append_int(mut out: List[UInt8], value: Int) -> None:
    if value == 0:
        out.append(UInt8(48))
        return
    var v = value
    var neg = v < 0
    if neg:
        v = -v
    var digits = List[UInt8](capacity=24)
    digits.resize(24, UInt8(0))
    var n = 0
    while v > 0:
        digits[n] = UInt8(48 + (v % 10))
        v //= 10
        n += 1
    if neg:
        out.append(UInt8(45))
    var k = n - 1
    while k >= 0:
        out.append(digits[k])
        k -= 1


struct Entry(Movable):
    var country: String
    var code: String
    var year: Int
    var dict: List[Int]

    def __init__(out self):
        self.country = String("")
        self.code = String("")
        self.year = 0
        self.dict = List[Int]()


def parse_field(buf: Span[UInt8, _], mut pos: Int) -> String:
    var n = len(buf)
    if pos >= n:
        return String("")
    if buf[pos] == Q:
        pos += 1
        var out = List[UInt8]()
        while pos < n:
            var c = buf[pos]
            if c == Q:
                if pos + 1 < n and buf[pos + 1] == Q:
                    out.append(Q)
                    pos += 2
                else:
                    pos += 1
                    break
            else:
                out.append(c)
                pos += 1
        if pos < n and buf[pos] == COMMA:
            pos += 1
        return String(StringSlice(unsafe_from_utf8=Span(out)))
    var start = pos
    while pos < n and buf[pos] != COMMA and buf[pos] != LF:
        pos += 1
    var end = pos
    if end > start and buf[end - 1] == CR:
        end -= 1
    var s = slice_str(buf, start, end - start)
    if pos < n and buf[pos] == COMMA:
        pos += 1
    elif pos < n and buf[pos] == LF:
        pos += 1
    return s


def skip_line(buf: Span[UInt8, _], mut pos: Int) -> None:
    var n = len(buf)
    if pos > 0 and pos <= n and buf[pos - 1] == LF:
        return
    while pos < n and buf[pos] != LF:
        pos += 1
    if pos < n:
        pos += 1


def code_valid(code: StringSlice) -> Bool:
    if code.byte_length() != 3:
        return False
    var b = code.as_bytes()
    for i in range(3):
        if b[i] < UInt8(65) or b[i] > UInt8(90):
            return False
    return True


def find_key(entries: List[Entry], code: StringSlice, year: Int) -> Int:
    for i in range(len(entries)):
        if entries[i].code == code and entries[i].year == year:
            return i
    return -1


def load_one(
    path: StringSlice,
    min_year: Int,
    spec: Int,
    nspec: Int,
    mut entries: List[Entry],
    mut values: List[String],
) raises -> None:
    var text = Path(path).read_text()
    var buf = List[UInt8]()
    buf.extend(String(text).as_bytes())
    var pos = 0
    skip_line(buf, pos)
    while pos < len(buf):
        if buf[pos] == LF:
            pos += 1
            continue
        var _entity = parse_field(buf, pos)
        var code = parse_field(buf, pos)
        var year_s = parse_field(buf, pos)
        var value = parse_field(buf, pos)
        if not code_valid(code):
            skip_line(buf, pos)
            continue
        var year = 0
        try:
            year = Int(year_s)
        except:
            skip_line(buf, pos)
            continue
        if year < min_year:
            skip_line(buf, pos)
            continue
        var idx = find_key(entries, code, year)
        if idx < 0:
            var e = Entry()
            e.country = String(_entity)
            e.code = String(code)
            e.year = year
            for _ in range(nspec):
                e.dict.append(-1)
            entries.append(e^)
            idx = len(entries) - 1
        elif entries[idx].country == "":
            entries[idx].country = String(_entity)
        if value == "":
            entries[idx].dict[spec] = -1
        else:
            values.append(String(value))
            entries[idx].dict[spec] = len(values) - 1
        skip_line(buf, pos)


def needs_quote(s: StringSlice) -> Bool:
    var b = s.as_bytes()
    for i in range(len(b)):
        if b[i] == COMMA or b[i] == Q or b[i] == LF:
            return True
    return False


def write_field(mut out: List[UInt8], s: StringSlice) -> None:
    if not needs_quote(s):
        out.extend(s.as_bytes())
        return
    out.append(Q)
    var b = s.as_bytes()
    for i in range(len(b)):
        if b[i] == Q:
            out.append(Q)
            out.append(Q)
        else:
            out.append(b[i])
    out.append(Q)


def less(a: Entry, b: Entry) -> Bool:
    if a.code != b.code:
        return a.code < b.code
    return a.year < b.year


def sort_entries(mut entries: List[Entry]) -> None:
    var n = len(entries)
    for i in range(n):
        var m = i
        for j in range(i + 1, n):
            if less(entries[j], entries[m]):
                m = j
        if m != i:
            entries.swap_elements(i, m)


def build_csv(
    entries: List[Entry],
    values: List[String],
    aliases: List[String],
) -> String:
    var out = List[UInt8]()
    append_str(out, "country,code,year")
    for a in range(len(aliases)):
        append_str(out, ",")
        append_str(out, aliases[a])
    out.append(LF)
    var nspec = len(aliases)
    for i in range(len(entries)):
        var any_val = False
        for s in range(nspec):
            if entries[i].dict[s] >= 0:
                any_val = True
                break
        if not any_val:
            continue
        write_field(out, entries[i].country)
        append_str(out, ",")
        append_str(out, entries[i].code)
        append_str(out, ",")
        append_int(out, entries[i].year)
        for s in range(nspec):
            append_str(out, ",")
            var vi = entries[i].dict[s]
            if vi >= 0:
                append_str(out, values[vi])
        out.append(LF)
    return String(StringSlice(unsafe_from_utf8=Span(out)))


def run_bundle(out_path: StringSlice, min_year: Int, spec_paths: List[String], aliases: List[String]) raises -> None:
    var nspec = len(aliases)
    var entries = List[Entry]()
    var values = List[String]()
    for s in range(nspec):
        load_one(spec_paths[s], min_year, s, nspec, entries, values)
    sort_entries(entries)
    var csv = build_csv(entries, values, aliases)
    Path(out_path).write_text(csv^)


def main() raises:
    var args = argv()
    if len(args) < 4:
        print("usage: owid_bundle OUT.csv MIN_YEAR SPEC...")
        return
    var out_path = String(args[1])
    var min_year = Int(args[2])
    var aliases = List[String]()
    var spec_paths = List[String]()
    for i in range(3, len(args)):
        var spec = String(args[i])
        var eq = -1
        for k in range(spec.byte_length()):
            if spec.as_bytes()[k] == UInt8(61):
                eq = k
                break
        if eq < 0:
            print("bad spec:", spec)
            return
        var sb = spec.as_bytes()
        var fname = String(StringSlice(unsafe_from_utf8=Span(sb)[0:eq]))
        var aname = String(
            StringSlice(unsafe_from_utf8=Span(sb)[eq + 1 : len(sb)])
        )
        aliases.append(aname^)
        spec_paths.append(fname^)
    run_bundle(out_path, min_year, spec_paths, aliases)
