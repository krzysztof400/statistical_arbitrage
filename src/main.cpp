#include <iostream>
#include "strategy.hpp"

int main() {
    double signal = arb::calculate_signal();
    std::cout << "It works: " << signal << std::endl;
    return 0;
}