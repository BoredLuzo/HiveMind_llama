def process(values):
    out = []
    for value in values:
        if value % 2 == 0:
            out.append(value)
    return out
