// Activate a DeckLink profile (e.g. '2dhd', two half-duplex sub-devices)
// on every device that offers it, and wait until it is active. Idempotent:
// devices already in that profile, or without it, are left alone.
//
// Build: g++ -std=c++11 -Wno-multichar -I<SDK>/Linux/include \
//   decklink-set-profile.cpp <SDK>/Linux/include/DeckLinkAPIDispatch.cpp -ldl -lpthread
#include <cstdio>
#include <cstring>
#include <unistd.h>
#include "DeckLinkAPI.h"

int main(int argc, char* argv[])
{
	if (argc != 2 || strlen(argv[1]) != 4) {
		fprintf(stderr, "Usage: %s <profile fourcc, e.g. 2dhd>\n", argv[0]);
		return 2;
	}
	const char* c = argv[1];
	BMDProfileID wanted = (BMDProfileID)((c[0] << 24) | (c[1] << 16) | (c[2] << 8) | c[3]);

	IDeckLinkIterator* it = CreateDeckLinkIteratorInstance();
	if (!it) {
		fprintf(stderr, "No DeckLink driver\n");
		return 1;
	}
	int failures = 0, index = 0;
	IDeckLink* dl = nullptr;
	while (it->Next(&dl) == S_OK) {
		IDeckLinkProfileManager* pm = nullptr;
		IDeckLinkProfile* profile = nullptr;
		if (dl->QueryInterface(IID_IDeckLinkProfileManager, (void**)&pm) == S_OK &&
		    pm->GetProfile(wanted, &profile) == S_OK) {
			bool active = false;
			profile->IsActive(&active);
			if (active) {
				printf("device %d: profile %s already active\n", index, c);
			} else if (profile->SetActive() != S_OK) {
				fprintf(stderr, "device %d: could not activate %s\n", index, c);
				failures++;
			} else {
				for (int i = 0; i < 50 && !active; i++) {
					usleep(100000);
					profile->IsActive(&active);
				}
				printf("device %d: profile %s %s\n", index, c, active ? "activated" : "did not become active");
				failures += active ? 0 : 1;
			}
		}
		if (profile) profile->Release();
		if (pm) pm->Release();
		dl->Release();
		index++;
	}
	it->Release();
	return failures ? 1 : 0;
}
