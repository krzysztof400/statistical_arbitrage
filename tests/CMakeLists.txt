enable_testing()

file(GLOB_RECURSE TEST_SOURCES "*.cpp")

add_executable(tests ${TEST_SOURCES})

target_link_libraries(tests PRIVATE stat_arb_lib Catch2::Catch2WithMain)

add_test(NAME AllTests COMMAND tests)