def rank(hypotheses):
    return sorted(
        hypotheses,
        key=lambda h: (h.priority, len(h.path)),
        reverse=True
    )
