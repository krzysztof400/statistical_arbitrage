file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS "lib/*.cpp")

set(PERFORMANCE_FLAGS -O3 -march=native -flto)

add_library(stat_arb_lib STATIC ${SOURCES})

target_compile_options(stat_arb_lib PRIVATE 
    $<$<CONFIG:Release>:${PERFORMANCE_FLAGS}>
)

target_link_options(stat_arb_lib PRIVATE $<$<CONFIG:Release>:-flto>)

target_include_directories(stat_arb_lib PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)

add_executable(bot main.cpp)

target_link_libraries(bot PRIVATE stat_arb_lib)
