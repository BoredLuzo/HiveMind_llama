from shapes import circle_area


def total_area(shapes):
    return sum(circle_area(item["r"]) for item in shapes)
