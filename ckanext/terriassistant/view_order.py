"""Keep the conventional map as the default without rewriting saved views."""


def prefer_standard_map(views):
    if not views or views[0].get("view_type") != "terriassistant":
        return views
    for index, view in enumerate(views[1:], 1):
        if view.get("view_type") == "terria_view":
            return [view] + views[:index] + views[index + 1:]
    return views
