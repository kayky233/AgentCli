#include <stdio.h>
#include "calculator.h"

int main(void) {
    int err = 0;
    int a = 6;
    int b = 3;
    printf("Demo calculator: %d + %d = %d\n", a, b, add(a, b));
    printf("Demo calculator: %d - %d = %d\n", a, b, subtract(a, b));
    printf("Demo calculator: %d * %d = %d\n", a, b, multiply(a, b));
    printf("Demo calculator: %d / %d = %d (err=%d)\n", a, b, divide(a, b, &err), err);
    return 0;
}

