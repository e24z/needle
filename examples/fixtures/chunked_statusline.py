def parse_status_packet(packet: dict[str, object]) -> str:
    mode = str(packet.get("mode") or "off")
    backend = str(packet.get("backend_status") or "cold")
    return f"{mode}:{backend}"


def noise_helper_00(value: int) -> int:
    return value + 0


def noise_helper_01(value: int) -> int:
    return value + 1


def noise_helper_02(value: int) -> int:
    return value + 2


def noise_helper_03(value: int) -> int:
    return value + 3


def noise_helper_04(value: int) -> int:
    return value + 4


def noise_helper_05(value: int) -> int:
    return value + 5


def noise_helper_06(value: int) -> int:
    return value + 6


def noise_helper_07(value: int) -> int:
    return value + 7


def noise_helper_08(value: int) -> int:
    return value + 8


def noise_helper_09(value: int) -> int:
    return value + 9


def noise_helper_10(value: int) -> int:
    return value + 10


def noise_helper_11(value: int) -> int:
    return value + 11


def cost_range_values(saved_tokens: int, input_cost: float, cache_read_cost: float) -> tuple[float, float]:
    high_rate = input_cost
    low_rate = cache_read_cost if cache_read_cost > 0 else input_cost
    return (
        saved_tokens * min(low_rate, high_rate),
        saved_tokens * max(low_rate, high_rate),
    )


def format_statusline_cost(saved_tokens: int, low: float, high: float, prunes: int) -> str:
    if saved_tokens <= 0 or high <= 0:
        return f"needle · pricing unavailable · {prunes} prunes"
    if low == high:
        estimate = f"~${high:.3f}"
    else:
        estimate = f"~${low:.3f}-${high:.3f}"
    return f"needle · {estimate} est input avoided · {prunes} prunes"
