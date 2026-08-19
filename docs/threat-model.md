# Threat model

Where the requirements in [SPEC.md](../SPEC.md) come from. Data collected 19 August 2026.

## What actually happens in hotels

A review of cases in Nha Trang and Vietnam more broadly:

- **Doc Let beach** — a snatch from a passing scooter; the bag was taken with the owner's wife
  standing right there. The police were no help.
- **Camellia Nha Trang** — $100 gone from the in-room safe; a key-and-code lock, and a pick can
  be made on the spot.
- **Novotel Nha Trang** — cash from the in-room safe.
- **Majestic Premium Nha Trang** — staff took money from a safe that had not been closed
  properly.
- **Hotel Majestic Saigon** — over 5 million VND from a locked safe; the hotel pulled its access
  log and confirmed a housekeeper had entered twice that day.

Not one stolen laptop in the sample. What gets stolen is cash.

## Three conclusions that shape the project

**1. The primary actor is staff.** A certified hotel security director puts staff behind
60–70 % of thefts: they already hold a master key and access to the safe. In the case reviewed
above, the log showed a master-key entry and the safe opened with a manual override device. So
the thing to defend against is not a forced door but an authorised entry — and the only
available way to record that is to take pictures.

**2. The in-room safe is weak protection.** Factory override codes of `0000` and `9999` are
supposed to be changed by the hotel and routinely are not; a "super-user" mode with code `999999`
is documented; many housings open with a piece of wire through the service hole in under two
minutes.

**3. Cash is stolen and laptops are not, and the reason is provability.** How much was in the
safe is one person's word against another's. A missing laptop is obvious, brings in the police,
the camera footage and the list of people who entered, and it has to be fenced. So a laptop in a
room is at less risk than an envelope of dollars — while out on the street the reverse is true,
and there it is exactly what a scooter snatch is after.

## Why a frame, and why a siren

A police report is required for insurance, but without witnesses or evidence a report may never
be issued at all. Hotels pull footage only against a report. Your own timestamped photograph and
a serial number recorded in advance are the two pieces of evidence that turn a hopeless
complaint into a working one. Going to the police, in turn, makes management markedly more
cooperative.

The siren addresses the other half of the same finding. If the actor is staff with legitimate
access, what they need is an exit nobody notices. Noise takes that away: a laptop that screams
when it is lifted cannot be carried out of a room, let alone through a corridor and a lobby, by
someone whose whole plan depends on going unremarked. It is not a physical barrier — see the
limits stated in [SPEC.md §1](../SPEC.md) — but it is the cheapest available way to make a quiet
exit impossible.

## Sources

- [TripAdvisor, Nha Trang forum — theft](https://www.tripadvisor.ru/ShowTopic-g293928-i9826-k8247476-Nha_Trang_Khanh_Hoa_Province.html)
- [TripAdvisor — safe theft, Hotel Majestic Saigon](https://www.tripadvisor.com/ShowUserReviews-g293925-d302780-r317829320-Hotel_Majestic_Saigon-Ho_Chi_Minh_City.html)
- [TripAdvisor — review of Majestic Premium, Nha Trang](https://www.tripadvisor.ru/ShowUserReviews-g293928-d13999826-r751178416-Majestic_Premium_Hotel-Nha_Trang_Khanh_Hoa_Province.html)
- [HuffPost — hotel safe override codes, share of thefts by staff](https://www.huffpost.com/entry/theres-a-secret-code-thieves-use-to-break-into-hotel-room-safes_n_5a9d64a5e4b0479c02558d63)
- [Corporate Travel Safety — how hotel safes are opened](https://www.corporatetravelsafety.com/safety-tips/hotel-room-safes-may-not-be-as-safe-as-you-thought-they-were/)
- [TripAdvisor — filing a police report in Vietnam](https://www.tripadvisor.com/ShowTopic-g293921-i8432-k4698372-Making_a_report_to_Police-Vietnam.html)
