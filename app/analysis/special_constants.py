SPECIAL_MEMBER_ROLES = (
    "车主",
    "画师",
    "章稿画师",
    "供稿人",
    "工具人",
)

SINGLE_PERSON_ROLES = frozenset(
    {
        "车主",
        "画师",
        "章稿画师",
        "供稿人",
    }
)

MULTI_PERSON_ROLES = frozenset(
    {
        "工具人",
    }
)