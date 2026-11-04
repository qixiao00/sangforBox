import csv
import io
from pathlib import Path

from af_app.app_control_analyzer import OBJECT_COLUMNS, ObjectIndex, analyze, parse_objects, parse_policies


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def object_csv(rows: list[list[str]]) -> str:
    fields = [OBJECT_COLUMNS["name"], OBJECT_COLUMNS["ranges"], OBJECT_COLUMNS["kind"], OBJECT_COLUMNS["refs"]]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["#####" + fields[0], *fields[1:]])
    writer.writerows(rows)
    return buffer.getvalue()


def policy_csv(*rows: str) -> str:
    return "\n".join(
        [
            "#名称,源区域,源网络对象,目的区域,目的网络对象,应用,服务,生效时间,动作（y，n）,启用状态（留空或y表示启用，n表示禁用）",
            *rows,
        ]
    )


def test_parses_mixed_export_and_excel_apostrophes(tmp_path: Path):
    objects = write(
        tmp_path / "objects.csv",
        object_csv([["'ip1", "'1.1.1.1", "IP地址", ""], ["'group", "", "地址组", "'ip1"]]),
    )
    parsed = parse_objects(objects)
    index = ObjectIndex(parsed)
    assert index.resolve("group") == frozenset({"1.1.1.1"})


def test_full_different_effect_shadow_is_reported(tmp_path: Path):
    objects = write(tmp_path / "objects.csv", "")
    policies = write(
        tmp_path / "policies.csv",
        policy_csv(
            "'wide,'wan,'All,'lan,'All,'全部,'any,'all-week,'n,'y",
            "'specific,'wan,'All,'lan,'All,'AppA,'any,'all-week,'y,'y",
        ),
    )
    result = analyze(parse_policies(policies), ObjectIndex(parse_objects(objects)))
    assert result["policy_analysis"][1]["status"] == "被前序不同效果策略完全遮蔽"
    assert result["overlaps"][0]["relation"] == "完全遮蔽_不同效果"


def test_merge_only_one_dimension_and_reject_cartesian_product(tmp_path: Path):
    objects = write(
        tmp_path / "objects.csv",
        object_csv(
            [
                ["'S1", "'10.0.0.1", "IP地址", ""],
                ["'S2", "'10.0.0.2", "IP地址", ""],
                ["'D1", "'10.0.1.1", "IP地址", ""],
                ["'D2", "'10.0.1.2", "IP地址", ""],
            ]
        ),
    )
    policies = write(
        tmp_path / "policies.csv",
        policy_csv(
            "'a,'wan,'S1,'lan,'D1,'AppA,'any,'all-week,'y,'y",
            "'b,'wan,'S2,'lan,'D1,'AppA,'any,'all-week,'y,'y",
            "'c,'wan,'S2,'lan,'D2,'AppA,'any,'all-week,'y,'y",
        ),
    )
    result = analyze(parse_policies(policies), ObjectIndex(parse_objects(objects)))
    pairs = {(row["left_name"], row["right_name"]) for row in result["merge_candidates"]}
    assert ("a", "b") in pairs
    assert ("b", "c") in pairs
    assert ("a", "c") not in pairs
