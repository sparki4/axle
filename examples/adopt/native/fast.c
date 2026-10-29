#include <stdint.h>
uint64_t sum_squares(uint64_t n) {
    uint64_t total = 0;
    for (uint64_t i = 0; i < n; i++) total += i * i;
    return total;
}
