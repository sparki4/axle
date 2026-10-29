# An ordinary Python module. AXLE calls it without any change to this file.
def with_tax(cents: int, percent: int) -> int:
    return cents + cents * percent // 100

def is_free(cents: int) -> bool:
    return cents == 0
