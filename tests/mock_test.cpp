#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>
#include "../src/include/strategy.hpp"

TEST_CASE("Signal calculation logic", "[strategy]") {
    
    SECTION("Basic signal returns expected mock value") {
        double signal = arb::calculate_signal();
        REQUIRE(signal == 42.0);
    }

    SECTION("Signal should be within realistic bounds") {
        double signal = arb::calculate_signal();
        
        REQUIRE(signal >= 0.0);
        REQUIRE(signal < 10000.0);
    }

    SECTION("Floating point precision safety") {
        double signal = arb::calculate_signal();
        REQUIRE_THAT(signal, Catch::Matchers::WithinAbs(42.0, 0.0001));
    }
}