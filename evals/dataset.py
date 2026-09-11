from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    question: str
    expected_phrases: tuple[str, ...]
    expected_source: str | None
    answerable: bool
    category: str

    def to_dict(self) -> dict:
        return asdict(self)


# expected_source is matched against citation["title"] in metrics.score_case(), and the
# app derives a document's title from its filename stem (no separate "display name"
# field) — so this must be "sample_handbook" (the stem of data/sample_handbook.md), not
# the human-readable "星云科技员工手册（示例）" heading inside the file. Regression:
# using the in-file heading here made recall_at_k/MRR/first_citation_accuracy score 0
# for every answerable case even when retrieval and the answer were both correct,
# because the title string the app actually returns never matched.
SEEDS = [
    ("年假", "员工的带薪年假有多少天？", ("5天",), "sample_handbook", True),
    ("年假申请", "申请年假需要提前多久？", ("三个工作日",), "sample_handbook", True),
    (
        "工作时间",
        "公司的标准上下班时间是什么？",
        ("9:00", "18:00"),
        "sample_handbook",
        True,
    ),
    ("午休", "午休时间是几点到几点？", ("12:00", "13:00"), "sample_handbook", True),
    ("报销期限", "差旅结束后多久内要提交报销？", ("十个工作日",), "sample_handbook", True),
    (
        "报销材料",
        "差旅报销必须准备哪些材料？",
        ("发票", "行程证明"),
        "sample_handbook",
        True,
    ),
    (
        "信息安全",
        "发现疑似数据泄露后应该联系谁？",
        ("信息安全部门",),
        "sample_handbook",
        True,
    ),
    ("餐补", "公司每天的餐补标准是多少？", (), None, False),
    ("停车", "员工停车位如何申请？", (), None, False),
    ("体检", "年度体检包含哪些项目？", (), None, False),
]

QUESTION_VARIANTS = (
    "{question}",
    "请问，{question}",
    "我想确认一下：{question}",
    "根据员工手册，{question}",
    "能否告诉我{question}",
    "新员工想了解，{question}",
    "帮我查一下：{question}",
    "在公司制度里，{question}",
    "请简要回答：{question}",
    "只依据知识库回答，{question}",
)

MECHANICAL_SEEDS = [
    (
        "联轴器用途",
        "联轴器用于连接哪些部件？",
        ("电机", "丝杆"),
        "110012034201 联轴器 功能用途说明书",
    ),
    ("联轴器原理", "联轴器两侧如何完成固定？", ("锁紧螺丝",), "110012034201 联轴器 功能用途说明书"),
    (
        "调压过滤器",
        "BFR20001 调压过滤器有什么用途？",
        ("过滤", "水气"),
        "120007017501-调压过滤器-BFR20001  功能用途说明书",
    ),
    (
        "电磁锁锁片",
        "D4SL-NK1S 电磁锁锁片用于做什么？",
        ("锁紧", "解锁"),
        "130013115201 电磁锁锁片 功能用途说明书",
    ),
    (
        "操作钥匙",
        "操作钥匙插入电磁锁后会触发什么？",
        ("信号",),
        "130013202701 操作钥匙  功能用途说明书",
    ),
    (
        "测力计用途",
        "XJC 测力计在设备上测量什么？",
        ("探头", "压力"),
        "130013272713 测力计 功能用途说明书",
    ),
    (
        "测力计原理",
        "测力计如何把压力转换为可检测的信号？",
        ("电阻应变片", "电信号"),
        "130013272713 测力计 功能用途说明书",
    ),
    (
        "压力模块用途",
        "压力传感器数字模块主要显示什么？",
        ("压力值",),
        "130303011801 压力传感器数字模块  功能用途说明书",
    ),
    (
        "压力模块原理",
        "压力数字模块怎样得到压力值？",
        ("压力信号", "电信号"),
        "130303011801 压力传感器数字模块  功能用途说明书",
    ),
    (
        "距离杆用途",
        "GB Camera 距离杆用于哪款产品的什么标定？",
        ("J717", "镭射治具"),
        "201BPAF12-900-003B GB Camera 距离杆 功能用途说明书",
    ),
    (
        "距离杆配合",
        "GB Camera 距离杆需要与什么配合校验相机距离？",
        ("距离标定块",),
        "201BPAF12-900-003B GB Camera 距离杆 功能用途说明书",
    ),
    (
        "RSL适配块",
        "RSL 适配块如何用于测试相机功能？",
        ("模拟", "检测距离"),
        "201BPAF50-903-004  RSL适配块 功能用途说明书",
    ),
    (
        "J720距离标定",
        "J720 镭射治具校验相机距离时要放入什么？",
        ("距离标定块", "Holder"),
        "201BPAF50-905-001 距离标定块 功能用途说明书",
    ),
    (
        "中心导向块",
        "相机中心块平行导向块与什么组件配合完成中心校验？",
        ("激光校验组件",),
        "201BPAF5-100-105 相机中心块平行导向块 功能用途说明书",
    ),
    (
        "十字标定块",
        "怎样判断十字激光的中心已经重合？",
        ("十字激光", "重合"),
        "201BPAF5-100-106  相机十字中心标定块 功能用途说明书",
    ),
    (
        "距离标定块",
        "BPAF52-903-003B 距离标定块模拟什么并标定什么？",
        ("产品", "相机", "安装距离"),
        "201BPAF52-903-003B  距离标定块 功能用途说明书",
    ),
    (
        "零度垫块",
        "0°垫块怎样让载具保持无倾斜？",
        ("底面", "0°"),
        "201BTTE1-500-001B 左0°垫块 功能用途说明书",
    ),
    (
        "左上垫块",
        "左上角度垫块怎样固定载具？",
        ("台阶", "左边"),
        "201BTTE1-500-006 左上角度垫块 功能用途说明书",
    ),
    (
        "右上垫块",
        "右上角度垫块主要夹紧载具的哪一部分？",
        ("右",),
        "201BTTE1-500-009 右上角度垫块 功能用途说明书",
    ),
    (
        "上插销座",
        "上插销座如何起到固定载具的作用？",
        ("上表面", "锁紧"),
        "201BTTE1-510-005 上插销座 功能用途说明书",
    ),
    (
        "载板转轴",
        "载板转轴与插销座固定后允许载具做什么？",
        ("转动", "倾斜"),
        "201BTTE1-510-006 载板转轴 功能用途说明书",
    ),
    (
        "锁片座",
        "锁片座为什么便于操作钥匙插入电磁锁？",
        ("固定", "操作钥匙"),
        "201BTTE31-200-002 锁片座 功能用途说明书",
    ),
    (
        "手机镭射治具",
        "CA39 手机镭射功能测试治具测试哪款产品？",
        ("N84",),
        "505BPAF-CA39 Alpha 功能用途说明书",
    ),
    (
        "触摸屏治具",
        "CW968 触摸屏菜单键测试治具如何判断 Pass 或 Fail？",
        ("压力值", "范围"),
        "505BTTE-CW968 触摸屏菜单键測試治具 功能用途說明書",
    ),
    (
        "触摸屏数据",
        "CW968 治具的测试数据保存在哪里？",
        ("中央控制计算机",),
        "505BTTE-CW968 触摸屏菜单键測試治具 功能用途說明書",
    ),
    (
        "触摸屏传输",
        "CW968 治具的测试数据通过什么方式传输？",
        ("USB",),
        "505BTTE-CW968 触摸屏菜单键測試治具 功能用途說明書",
    ),
    (
        "触摸屏固定",
        "CW968 测试治具通过什么部件固定产品进行测试？",
        ("真空吸盘",),
        "505BTTE-CW968 触摸屏菜单键測試治具 功能用途說明書",
    ),
]

MECHANICAL_VARIANTS = ("{question}", "请依据机械文档回答：{question}")

MECHANICAL_UNANSWERABLE = [
    ("保养周期", "联轴器每隔多少个月必须更换一次？"),
    ("润滑油", "载板转轴指定使用哪一种润滑油？"),
    ("质保期限", "CW968 测试治具的质保期是多久？"),
    ("采购价格", "BFR20001 调压过滤器的采购单价是多少？"),
    ("供应商电话", "测力计供应商的联系电话是什么？"),
    ("额定寿命", "电磁锁锁片的额定使用寿命是多少次？"),
    ("环境温度", "CA39 治具允许工作的环境温度范围是多少？"),
    ("校准周期", "压力传感器数字模块要求多久校准一次？"),
    ("备件库存", "当前仓库还有多少个相机距离标定块？"),
    ("培训要求", "操作 CW968 治具前必须完成哪门培训？"),
]


def build_dataset() -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for seed_index, (category, question, phrases, source, answerable) in enumerate(SEEDS):
        for variant_index, template in enumerate(QUESTION_VARIANTS):
            cases.append(
                EvaluationCase(
                    id=f"case-{seed_index + 1:02d}-{variant_index + 1:02d}",
                    question=template.format(question=question),
                    expected_phrases=phrases,
                    expected_source=source,
                    answerable=answerable,
                    category=category,
                )
            )
    return cases


def build_mechanical_dataset() -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for seed_index, (category, question, phrases, source) in enumerate(MECHANICAL_SEEDS):
        for variant_index, template in enumerate(MECHANICAL_VARIANTS):
            cases.append(
                EvaluationCase(
                    id=f"mechanical-{seed_index + 1:02d}-{variant_index + 1:02d}",
                    question=template.format(question=question),
                    expected_phrases=phrases,
                    expected_source=source,
                    answerable=True,
                    category=category,
                )
            )
    for seed_index, (category, question) in enumerate(MECHANICAL_UNANSWERABLE, start=1):
        cases.append(
            EvaluationCase(
                id=f"mechanical-na-{seed_index:02d}",
                question=question,
                expected_phrases=(),
                expected_source=None,
                answerable=False,
                category=category,
            )
        )
    return cases
