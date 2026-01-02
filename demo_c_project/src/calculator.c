#include "calculator.h"

int add(int a, int b) {
    return a + b;
}

int subtract(int a, int b) {
    return a - b;
}

int multiply(int a, int b) {
    return a * b;
}

int calculator_mod(int a, int b, int* error) {
    if (b == 0) {
        if (error) *error = 1;
        return 0;
    }
    if (error) *error = 0;
    return a % b;
}

int divide(int a, int b, int* error) {
    if (b == 0) {
        if (error) {
            *error = 1;
        }
        return 0;
    }
    if (error) {
        *error = 0;
    }
    return a / b;
}

