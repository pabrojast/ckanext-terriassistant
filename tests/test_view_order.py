from ckanext.terriassistant.view_order import prefer_standard_map


def view(kind, key):
    return {"view_type": kind, "id": key, "terria_state_json": "saved-style"}


def test_legacy_assistant_does_not_displace_terria_or_lose_saved_state():
    assistant, text, terrain = view("terriassistant", "a"), view("text_view", "b"), view("terria_view", "c")
    original = [assistant, text, terrain]
    result = prefer_standard_map(original)
    assert result == [terrain, assistant, text]
    assert original == [assistant, text, terrain]
    assert result[1] is assistant and assistant["terria_state_json"] == "saved-style"


def test_non_assistant_default_and_order_are_preserved():
    original = [view("pdf_view", "p"), view("terriassistant", "a"), view("terria_view", "t")]
    assert prefer_standard_map(original) is original


def test_missing_standard_map_does_not_create_a_view():
    original = [view("terriassistant", "a"), view("text_view", "b")]
    assert prefer_standard_map(original) is original
    assert prefer_standard_map([]) == []


def test_first_normal_map_wins_without_reordering_other_saved_maps():
    original = [view("terriassistant", "a"), view("terria_view", "t1"), view("terria_view", "t2")]
    assert [v["id"] for v in prefer_standard_map(original)] == ["t1", "a", "t2"]
