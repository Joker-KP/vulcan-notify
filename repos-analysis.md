```
W repos.log masz adresy innych podobnych rozwiązań, które umożliwiają dostęp do eduVulcan. Przejrzyj je w poszukiwaniu ciekawych rozwiązań (także technicznie) - featurów, które można by zastosować tutaj (bo teraz ich nie ma). Nie modyfikuj żadnych plików.
```

Przejrzałem wszystkie sześć repozytoriów z `repos.log`, ich dokumentację i wybrane implementacje, a następnie porównałem je z lokalnym kodem. **Najwięcej wartości widzę w rozszerzeniu wiadomości, synchronizacji kolejnych kategorii danych i eksperymencie z mobilnym API HebeCE.** Nie zmieniłem żadnych plików; repozytorium pozostaje czyste.

| Projekt | Co jest szczególnie ciekawe |
|---|---|
| [kapi2289/vulcan-api](https://github.com/kapi2289/vulcan-api) | Podpisywanie żądań mobilnych, modele danych i pobieranie zmian przez `lastSyncDate`. To biblioteka dla UONET+/Dzienniczka VULCAN; jej zgodności z obecnym eduVULCAN nie należy zakładać. |
| [bbrjpl1310b/iris](https://github.com/bbrjpl1310b/iris) | Najszerszy zestaw modeli i metod: Hebe oraz HebeCE, statystyki frekwencji, zebrania, wycieczki, dni wolne, jadłospisy. README wskazuje ograniczenia funkcji premium w eduVULCAN. |
| [DzienniczekSzpontniczek](https://github.com/szponciciel04/DzienniczekSzpontniczek) | Połączenie mobilnego API z przeglądarkowym modułem wiadomości; osobne skrzynki i foldery, metody wysyłania i oznaczania wiadomości jako przeczytanych. |
| [maalos/open_eduVulcan](https://github.com/maalos/open_eduVulcan) | Przydatne wskazówki dotyczące mobilnych endpointów. Kod wymaga zewnętrznie pozyskanych podpisów/certyfikatów i ponownie wykorzystuje nagłówki podpisu oraz daty — traktowałbym go jako materiał do rozpoznania API. |
| [09kz/eduvulcan-api](https://github.com/09kz/eduvulcan-api) | Rejestracja urządzenia z tokenów `/api/ap`, własne podpisy kryptograficzne, rejestracja dla wielu tenantów i opcjonalny klucz dostępu do lokalnego API. |
| [tumski/eduvulcan-cli](https://github.com/tumski/eduvulcan-cli) | Znormalizowane migawki konkretnego dnia, profile zakresu pobierania oraz blokada całego procesu z odzyskiwaniem porzuconej blokady. |

Najbardziej użyteczne pomysły do zastosowania tutaj:

1. **Pełniejsza synchronizacja wiadomości: starsze strony, foldery i załączniki.**

   Szpontniczek przekazuje kursor `idLastWiadomosc` i identyfikator konkretnej skrzynki oraz udostępnia odebrane, wysłane i usunięte wiadomości. Iris przechowuje także identyfikator wątku, odbiorców i listę załączników. [Obsługa folderów](https://github.com/szponciciel04/DzienniczekSzpontniczek/blob/main/composeApp/src/commonMain/kotlin/io/github/szpontium/api/prometheus/PrometheusMessagesApi.kt), [model wiadomości](https://github.com/bbrjpl1310b/iris/blob/master/iris/models/_message.py).

   U nas [client.py](/home/joker/vulcan/vulcan-poc/src/vulcan_notify/client.py:604) pobiera pierwszą stronę, domyślnie 50 wiadomości. Szczegóły sprowadza do treści HTML; zachowujemy tylko informację, że załączniki istnieją. Proponowałbym ograniczony import historii z paginacją, potem synchronizację do napotkania znanych wiadomości, oraz zapis metadanych załączników. **Samo zwiększenie `pageSize` nie zapewnia kompletności.**

2. **Ogłoszenia, zebrania i dni wolne jako normalne synchronizowane kategorie.**

   Iris ma konkretne metody i modele zebrań, ogłoszeń, wycieczek, wydarzeń oraz dni wolnych. Tumski pobiera dni wolne i ogłoszenia przez ten sam rodzaj API przeglądarkowego, którego używamy. [Metody Iris](https://github.com/bbrjpl1310b/iris/blob/master/iris/api/_base.py), [pobieranie w CLI](https://github.com/tumski/eduvulcan-cli/blob/main/src/fetch.ts).

   To dobry kierunek z niewielką liczbą niewiadomych: lokalny [kontrakt API](/home/joker/vulcan/vulcan-poc/docs/eduvulcan/README.md) zawiera już m.in. `Zebrania`, `DniWolne`, `Informacje` i `Nauczyciele`. Ogłoszenia mają nawet metodę w kliencie, lecz nie przechodzą przez standardowy sync → baza → zdarzenia. W pierwszej kolejności wybrałbym ogłoszenia, zebrania i dni wolne; jadłospisy czy przedszkole zależą od potrzeb konta.

3. **Rozszerzenie kalendarza ICS o sprawdziany, zadania i zebrania.**

   To moja propozycja wynikająca z dostępnych danych. Obecny [generator ICS](/home/joker/vulcan/vulcan-poc/src/vulcan_notify/ics.py:121) obejmuje plan lekcji. Sprawdziany i zadania są już zapisane lokalnie, a zebrania w Iris mają termin, miejsce, agendę i link online. [Model zebrania](https://github.com/bbrjpl1310b/iris/blob/master/iris/models/_meeting.py).

   Można więc dodać osobne subskrypcje lub wybór kategorii w jednym feedzie. Dałoby to kalendarz obowiązków szkolnych działający również w obecnym wdrożeniu Linux/Docker.

4. **Statystyki frekwencji i wykrywanie zmiany statusu obecności.**

   Iris obsługuje statystyki miesięczne i przedmiotowe, a typ frekwencji opisuje flagami takimi jak nieobecność, spóźnienie i usprawiedliwienie. [Statystyki przedmiotowe](https://github.com/bbrjpl1310b/iris/blob/master/iris/models/_presence_subject_stats.py), [typ frekwencji](https://github.com/bbrjpl1310b/iris/blob/master/iris/models/_presence_type.py).

   U nas frekwencja jest synchronizowana, ale brakuje jej endpointu HTTP i statystyk. [Differ](/home/joker/vulcan/vulcan-poc/src/vulcan_notify/differ.py:157) zgłasza tylko nowe wpisy; zmiana istniejącej nieobecności na usprawiedliwioną pozostaje bez powiadomienia. Przydatne byłyby miesięczne zestawienia, procent obecności na przedmiotach i zdarzenia takich korekt. Numerów kategorii z mobilnego API trzeba jednak zweryfikować względem API webowego.

5. **Oceny z wartością liczbową i średnimi otrzymanymi ze źródła.**

   Mobilny model Iris zawiera osobno tekst oceny i `Value`, komentarz, czas utworzenia/modyfikacji oraz autora zmiany. Biblioteka ma też endpoint średnich. [Model oceny](https://github.com/bbrjpl1310b/iris/blob/master/iris/models/_grade.py), [endpoint średnich](https://github.com/bbrjpl1310b/iris/blob/master/iris/api/_base.py).

   Obecnie [api.py](/home/joker/vulcan/vulcan-poc/src/vulcan_notify/api.py:274) przelicza tekst ocen, stosując stałe `+0.5` i `−0.25`. Wartość źródłowa mogłaby poprawić zgodność między szkołami. Nowymi funkcjami byłyby też komentarze i historia korekt. **Średnie i oceny przewidywane/końcowe już mamy** — rozszerzenie dotyczyłoby dokładności i dodatkowych informacji.

6. **Opcjonalny dostęp przez zarejestrowane urządzenie HebeCE.**

   Iris i 09kz pokazują przebieg: pobranie tokenów z `/api/ap`, wygenerowanie certyfikatu/klucza urządzenia, rejestracja przez `mobile/register/jwt`, następnie podpisywanie żądań. [Instrukcja Iris](https://github.com/bbrjpl1310b/iris/blob/master/docs/getting-started.md), [rejestracja 09kz](https://github.com/09kz/eduvulcan-api/blob/main/src/api/register.ts).

   To największy pomysł techniczny: dodatkowy sposób dostępu do danych, potencjalnie ograniczający zależność od sesji przeglądarkowej. Mobilne modele oferują też stabilne ID lekcji, a żądania parametry `lastSyncDate`, `lastId`, `pageSize`. Mogłoby to usprawnić synchronizację i utrzymać tożsamość lekcji po zmianie godziny lub przedmiotu.

   Zacząłbym od eksperymentu porównującego oba źródła dla jednego ucznia. Trzeba sprawdzić pokrycie danych i uprawnienia. Przy pobieraniu przyrostowym brak rekordu w odpowiedzi **nie oznacza usunięcia**; obecne wykrywanie brakujących elementów wymaga pełnej odpowiedzi dla porównywanego zakresu.

7. **Blokada całej synchronizacji.**

   Tumski blokuje cały proces i wykrywa porzucone blokady. [Implementacja](https://github.com/tumski/eduvulcan-cli/blob/main/scripts/fetch-with-retries.sh).

   U nas blokada zabezpiecza profil Chromium, a pętla wykonuje sync sekwencyjnie, lecz osobno uruchomione procesy nadal mogą synchronizować równolegle. Blokada związana z bazą, obejmująca również dostarczanie powiadomień, ograniczyłaby wyścigi przy ustalaniu baseline i opróżnianiu kolejek. W Dockerze można wykorzystać już stosowane `flock`.

8. **Zbiorczy widok dnia i opcjonalne uwierzytelnienie HTTP API.**

   Z Tumski warto przejąć format migawki dnia: plan, zadania, wiadomości, dni wolne i stan kompletności. [Format danych](https://github.com/tumski/eduvulcan-cli/blob/main/src/types.ts). Tutaj mógłby to być `/api/day?student_key=…&date=…`, składany z SQLite — wygodny dla Homey, dashboardu i podsumowania „co jutro w szkole”.

   Z 09kz przydatny jest prosty, opcjonalny `X-API-Key`. [Middleware](https://github.com/09kz/eduvulcan-api/blob/main/src/middleware/apiKey.ts). Lokalne API obecnie nie ma takiej kontroli dostępu.

Osobnym kierunkiem są **odpowiadanie na wiadomości i usprawiedliwianie nieobecności**: Szpontniczek ma odpowiednie metody API. To rozszerzyłoby aplikację o działania w dzienniku, wymagające jawnego wywołania przez użytkownika. [Metody zapisu](https://github.com/szponciciel04/DzienniczekSzpontniczek/blob/main/composeApp/src/commonMain/kotlin/io/github/szpontium/api/hebe/SzpontApi.kt).

**Moja kolejność wdrażania:** wiadomości i załączniki → ogłoszenia/zebrania/dni wolne → rozszerzony ICS → frekwencja. Blokadę sync można zrobić niezależnie jako małą poprawę techniczną. HebeCE zasługuje na osobny eksperyment przed decyzją o integracji.

To ocena kodu i dokumentacji; nie sprawdzałem tych rozwiązań na żywym koncie eduVULCAN.
