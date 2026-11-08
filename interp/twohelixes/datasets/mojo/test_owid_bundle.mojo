from owid_bundle import run_bundle, parse_field, code_valid
from std.pathlib import Path


def check(name: StringSlice, got: StringSlice, want: StringSlice) raises -> None:
    if got != want:
        print("FAIL", name)
        print(" got =", got)
        print(" want =", want)
        raise Error("test failed: " + String(name))
    print("ok", name)


def to_buf(s: StringSlice) -> List[UInt8]:
    var b = List[UInt8]()
    b.extend(s.as_bytes())
    return b^


def test_code_valid() raises -> None:
    if not code_valid("USA"):
        raise Error("USA should be valid")
    if code_valid("OWID_WRL"):
        raise Error("aggregate should be invalid")
    if code_valid(""):
        raise Error("empty should be invalid")
    if code_valid("us"):
        raise Error("lowercase should be invalid")
    if code_valid("US"):
        raise Error("short should be invalid")
    print("ok code_valid")


def test_parse_quoted_comma() raises -> None:
    var buf = to_buf('"Korea, South",KOR,2020,84.1\n')
    var pos = 0
    var entity = parse_field(buf, pos)
    check("quoted comma entity", entity, "Korea, South")
    var code = parse_field(buf, pos)
    check("quoted comma code", code, "KOR")
    var year = parse_field(buf, pos)
    check("quoted comma year", year, "2020")
    var value = parse_field(buf, pos)
    check("quoted comma value", value, "84.1")


def test_bundle() raises -> None:
    var out = "interp/twohelixes/datasets/mojo/testdata/out.csv"
    var paths = List[String]()
    paths.append(String("interp/twohelixes/datasets/mojo/testdata/a.csv"))
    paths.append(String("interp/twohelixes/datasets/mojo/testdata/b.csv"))
    var aliases = List[String]()
    aliases.append(String("a"))
    aliases.append(String("b"))
    run_bundle(out, 1990, paths, aliases)
    var text = Path(out).read_text()
    var want = (
        "country,code,year,a,b\n"
        "France,FRA,1990,76.9,68.0\n"
        "France,FRA,2020,82.5,\n"
        "Japan,JPN,2020,,99.9\n"
        '"Korea, South",KOR,1990,70.5,50.0\n'
        '"Korea, South",KOR,2020,84.1,51.0\n'
    )
    check("bundle output", text, want)
    if "OWID" in text:
        raise Error("aggregate not dropped")
    check("aggregate dropped", String("OWID" in text), String(False))
    if "69.0" in text:
        raise Error("pre-1990 row not dropped")
    check("pre-1990 dropped", String("69.0" in text), String(False))
    if "AAA" in text:
        raise Error("empty-value row not dropped")
    check("empty-value row dropped", String("AAA" in text), String(False))


def test_negative_control() raises -> None:
    var buf = to_buf("France,FRA,1990,76.9\n")
    var pos = 0
    var entity = parse_field(buf, pos)
    if entity == "Korea, South":
        raise Error("negative control: France parsed as Korea")
    print("ok negative control")


def main() raises:
    test_code_valid()
    test_parse_quoted_comma()
    test_bundle()
    test_negative_control()
    print("all owid_bundle tests passed")
