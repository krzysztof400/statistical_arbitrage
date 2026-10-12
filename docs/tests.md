# Plan badań wydajnościowych: kolejka SPSC (lock-free) w silniku stat-arb

Dokument roboczy do pracy inżynierskiej. Opisuje eksperymenty wydajnościowe: czynniki, warianty kodu, metodologię pomiarów i analizę wyników.

## 1. Cel i pytania badawcze

Cel: ocenić, jak decyzje projektowe w lock-free kolejce SPSC wpływają na throughput i rozkład latencji przekazywania wiadomości między wątkami oraz latencję hot patha silnika.

- **P1.** Który z czynników (memory ordering, padding, cached indices, indeksowanie, strategia czekania) ma największy wpływ na latencję i throughput? Czy występują interakcje między nimi?
- **P2.** Czy kolejka lock-free daje realną przewagę nad `mutex + condvar` i w jakich warunkach (obciążenie, rozmiar wiadomości, topologia rdzeni)?
- **P3.** Jak rośnie rozkład latencji end-to-end (p50/p99/p99.9) przy rosnącej liczbie wiadomości/s i gdzie jest punkt załamania?
- **P4.** Ile kosztuje false sharing i czy `alignas(64)` wystarcza, czy potrzeba 128 B?
- **P5 (most do backtestu).** Jak latencja wykonania wpływa na PnL strategii?

## 2. Czynniki eksperymentu (plan czynnikowy)

Wszystkie czynniki to warianty **tej samej** kolejki atomowej, wybierane parametrami szablonu. Architektura systemu się nie zmienia.

| Czynnik | Poziomy | Jak wchodzi do kodu |
|---|---|---|
| Memory ordering | `seq_cst`, `acquire/release` | parametry szablonu `StoreOrd`, `LoadOrd` |
| Padding / alignment | 8 B (brak), 64 B, 128 B | parametr `Align` w `alignas` |
| Cached head/tail | wyłączone, włączone | `bool` + `if constexpr` |
| Indeksowanie | modulo, maska `& (N-1)` | `bool` + `if constexpr` (maska wymaga N = 2^k) |
| Strategia czekania | busy spin, spin z `pause`, `yield` | klasa-polityka jako parametr szablonu |
| Rozmiar wiadomości | 16 B, 64 B, 256 B | parametr `T` |
| Pojemność bufora | 256, 4096, 65536 | parametr `N` |
| Topologia rdzeni | SMT sibling, ten sam die/CCX, różne CCX/sockety | argumenty harnessu (`sched_setaffinity`), nie kod kolejki |

**Punkt odniesienia (nie czynnik):** `std::queue` + `std::mutex` + `std::condition_variable` z tym samym interfejsem `push/pop`, uruchamiana w tym samym harnessie. Nie mnoży liczby konfiguracji w gridzie.

**Poza gridem:** `relaxed` dla indeksu drugiego wątku. Psuje gwarancję widoczności danych i daje niepoprawną kolejkę. Można osobno opisać w podrozdziale o poprawności.

### Rozmiar planu

- Rdzeń: ordering (2) × padding (3) × cached (2) = **12 wariantów**.
- Start: jedna topologia, jeden rozmiar wiadomości, jedna pojemność. Pozostałe osie dodawać tylko, jeśli zmieniają wynik.
- Pełny grid (z topologią i rozmiarem wiadomości) to ~100 konfiguracji × 20-30 powtórzeń. Skrypt robi to sam.
- Niepoprawne lub bezsensowne kombinacje wyrzucić z gridu przed pomiarem.

## 3. Szkielet kodu

```cpp
#include <atomic>
#include <cstddef>
#include <thread>
#include <immintrin.h>

struct SpinWait  { static void wait() noexcept {} };
struct PauseWait { static void wait() noexcept { _mm_pause(); } };
struct YieldWait { static void wait() noexcept { std::this_thread::yield(); } };

template <typename T, size_t N,
          std::memory_order StoreOrd = std::memory_order_release,
          std::memory_order LoadOrd  = std::memory_order_acquire,
          size_t Align = 64,
          bool CachedIndices = true,
          bool MaskIndex = true>
class SpscQueue {
    static_assert(!MaskIndex || (N & (N - 1)) == 0, "N must be a power of two");
    static_assert(StoreOrd == std::memory_order_release ||
                  StoreOrd == std::memory_order_seq_cst);
    static_assert(LoadOrd == std::memory_order_acquire ||
                  LoadOrd == std::memory_order_seq_cst);

    // indeksy rosną monotonicznie; rozmiar = tail - head
    alignas(Align) std::atomic<size_t> head_{0};  // pisze konsument
    alignas(Align) std::atomic<size_t> tail_{0};  // pisze producent
    alignas(Align) size_t cached_head_{0};        // lokalne dla producenta
    alignas(Align) size_t cached_tail_{0};        // lokalne dla konsumenta
    alignas(Align) T buf_[N];

    static constexpr size_t idx(size_t i) noexcept {
        if constexpr (MaskIndex) return i & (N - 1);
        else return i % N;
    }

public:
    bool try_push(const T& v) noexcept {
        const size_t t = tail_.load(std::memory_order_relaxed);
        if constexpr (CachedIndices) {
            if (t - cached_head_ == N) {
                cached_head_ = head_.load(LoadOrd);
                if (t - cached_head_ == N) return false;
            }
        } else {
            if (t - head_.load(LoadOrd) == N) return false;
        }
        buf_[idx(t)] = v;
        tail_.store(t + 1, StoreOrd);
        return true;
    }

    bool try_pop(T& out) noexcept {
        const size_t h = head_.load(std::memory_order_relaxed);
        if constexpr (CachedIndices) {
            if (h == cached_tail_) {
                cached_tail_ = tail_.load(LoadOrd);
                if (h == cached_tail_) return false;
            }
        } else {
            if (h == tail_.load(LoadOrd)) return false;
        }
        out = buf_[idx(h)];
        head_.store(h + 1, StoreOrd);
        return true;
    }
};

// Warianty (przykłady)
using Q_seqcst = SpscQueue<Msg, 4096, std::memory_order_seq_cst,
                           std::memory_order_seq_cst, 8,  false>;
using Q_acqrel = SpscQueue<Msg, 4096, std::memory_order_release,
                           std::memory_order_acquire, 8,  false>;
using Q_pad64  = SpscQueue<Msg, 4096, std::memory_order_release,
                           std::memory_order_acquire, 64, false>;
using Q_cached = SpscQueue<Msg, 4096, std::memory_order_release,
                           std::memory_order_acquire, 64, true>;
```

Uwagi do kodu:

- Własny indeks (`tail_` u producenta, `head_` u konsumenta) czytany `relaxed`. Indeks drugiego wątku czytany z `LoadOrd`, zapis własnego z `StoreOrd`.
- Jeden szablonowy benchmark `run_benchmark<Q>()` wołany dla każdego wariantu, w losowej kolejności w obrębie jednego uruchomienia.
- Flagi kompilatora (`-O2` vs `-O3`, `-march=native`) to osobna, opcjonalna oś wymagająca przebudowania. Priorytetem są warianty kodu.

## 4. Eksperymenty

### E1 + E3: plan czynnikowy kolejki (ordering, padding, cached, indeksowanie, waiting)

- Mierz: throughput (ops/s) i latencję producent → konsument (rozkład).
- Na x86 `acquire` load i `release` store kompilują się do zwykłego `mov`. Różnica względem `seq_cst` dotyczy głównie store'ów (`xchg`/`mfence`). RMW jest pełną barierą niezależnie od ordering. **Sprawdź asembler** (Compiler Explorer, `objdump`) i opisz w pracy. Jeśli dostępne ARM (Graviton, Apple Silicon, Raspberry Pi), wynik będzie ciekawszy i da porównanie architektur.
- False sharing: head i tail w jednej linii vs `alignas(64)` vs `alignas(128)` (sąsiednia linia bywa prefetchowana parami). Mikrotest: N wątków inkrementujących sąsiednie liczniki, z paddingiem i bez.
- Dowód mechanizmu: `perf c2c` (zdarzenia HITM), liczniki cache misses. Samo „jest szybciej" to obserwacja, a „jest szybciej, bo HITM spadło o X%" to wyjaśnienie.
- Poprawność przed wydajnością: ThreadSanitizer (`-fsanitize=thread`), stress test z weryfikacją kolejności i braku utraty wiadomości.

### E2: latencja hot patha pod rosnącym obciążeniem

- Replay danych z kontrolowaną prędkością (1x, 2x, 5x, 10x, ... do saturacji).
- Znaczniki czasu na granicach etapów: odbiór → parsowanie → aktualizacja księgi → sygnał → decyzja/zlecenie. Daje rozbicie latencji na komponenty.
- **Obciążenie open-loop:** wiadomości wysyłane według harmonogramu, niezależnie od tego, czy system nadąża. Latencja liczona od zamierzonego momentu wysłania. Closed-loop ukrywa kolejkowanie i zaniża tail (coordinated omission).
- Wykres: p50/p99/p99.9 vs throughput (kolano załamania).
- Atrybucja ogona: `perf stat` (cache misses, context switches, page faults), sprawdź, czy ogon wynika z kolejkowania, alokacji, cache missów czy przerwań.
- Warianty: hot path z alokacją vs pre-alokowane pule, z logowaniem vs bez, cold path (np. rekalkulacja hedge ratio) na osobnym rdzeniu vs na tym samym.

### E4 (most): latencja a PnL

- W backtesterze dodaj parametr opóźnienia wykonania (0, 1, 10, 100 ms) i zmierz degradację PnL/Sharpe'a.
- Użyj zmierzonych w E2 rozkładów latencji jako realistycznych wartości wejściowych.

## 5. Infrastruktura pomiarowa (zrobić najpierw)

- **Timer:** `rdtsc`/`rdtscp` (z `lfence`) skalibrowany do ns. Sprawdź `constant_tsc` i `nonstop_tsc` w `/proc/cpuinfo`. Pomiar między rdzeniami wymaga zsynchronizowanego TSC (na jednym sockecie zwykle jest).
- **Środowisko:** pinning wątków (`sched_setaffinity`), governor `performance`, najlepiej `isolcpus`/`nohz_full` dla rdzeni pomiarowych. Zanotuj turbo, SMT, wersję kernela, kompilator i flagi. Natywny Linux, nie WSL ani VM.
- **Rejestrowanie:** HdrHistogram, nie średnie. Raportuj p50/p99/p99.9/max.
- **Powtarzalność:** warmup, min. 20-30 uruchomień, przedziały ufności (bootstrap), randomizowana kolejność wariantów (dryf termiczny).
- **Odtwarzalność:** jedno polecenie generujące wszystkie wykresy z surowych danych. Opis środowiska zapisywany automatycznie razem z wynikami.

## 6. Analiza wyników

- Efekty główne i **interakcje** (np. padding może mieć duże znaczenie bez cached indices, a z nimi mniejsze). Wykresy interakcji, nie tylko tabele.
- Które czynniki tłumaczą największą część wariancji (ANOVA lub efekty główne z przedziałami ufności).
- Wyniki negatywne dokumentować uczciwie (np. brak różnicy ordering na x86, mutex wygrywa przy niskim obciążeniu).
- Mikrobenchmark vs end-to-end: sprawdź, czy wygrana w izolacji utrzymuje się w całym systemie.

## 7. Pułapki

1. **Zła metodologia unieważnia wyniki:** brak warmupu, brak pinningu, turbo/frequency scaling, pomiar `std::chrono` w pętli zamiast `rdtsc`, VM/WSL.
2. **Mikrobenchmarki mogą kłamać:** wygrana w izolowanym teście nie gwarantuje wygranej w systemie.
3. **Lock-free nie znaczy szybciej:** pokaż przypadki, gdzie lock wygrywa lub jest równy.
4. **Poprawność przed wydajnością:** niepoprawny wariant (np. błędne `relaxed`) nie jest „szybszą kolejką".
5. **Rozdmuchany grid:** każdy dodatkowy czynnik mnoży konfiguracje do opisania i zinterpretowania. Zaczynaj od małego rdzenia.
6. **Strategia czekania zmienia charakter pomiaru:** `yield` robi syscall i wprowadza zmienność, mutex+condvar usypia wątek (budzenie przez kernel). Porównuj osobno, z opisem co dokładnie mierzysz.

## 8. Kolejność prac

1. Infrastruktura pomiarowa (timer, harness, histogramy, skrypt wykresów).
2. `Q_seqcst` i `Q_acqrel` + analiza asemblera + testy TSan.
3. Dodanie paddingu i cached indices, grid 12 wariantów na jednej topologii.
4. Baseline mutex + condvar.
5. Eksperyment E2 (open-loop, rozbicie latencji na etapy).
6. Dodatkowe osie (topologia, rozmiar wiadomości, pojemność, waiting) tylko jeśli zmieniają wynik.
7. E4: wpływ latencji na PnL.
8. Analiza statystyczna i pisanie rozdziału wyników równolegle z pomiarami.

Szacunek czasu: infrastruktura 10-15 h, E1+E3 20-30 h, E2 15-25 h, razem 45-70 h (bez pisania).