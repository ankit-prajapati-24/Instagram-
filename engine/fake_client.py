"""A stand-in for OmniRouteClient.

Exists because a clean OmniRoute install has zero provider nodes configured, so
no chat completion can be served until one provider key is added. Rather than
leave the whole pipeline unverifiable until then, every stage downstream of the
brain is exercised against this: real edge-tts, real ffmpeg, real ASS, real
SQLite, fake LLM.

It is also the fixture the agent tests run against, so prompt-contract changes
are caught without spending credits.
"""

from __future__ import annotations

from engine.omniroute import ChatResult, CostRecord, ModerationResult

# A real Hinglish script for the reference topic, and the only example in the
# codebase of what the script agent is supposed to produce. It obeys the
# retention rules in the spec: a specific hook, no banned openers, a "lekin"
# reversal, and a loop-closing final line.
#
# Two properties matter beyond the story:
#   * it hits the word budget (~113 spoken words), because runtime follows
#     word count and the offline harness checks the rendered duration. It was
#     136 while words_per_second was set to 3.03; Piper actually speaks 2.29,
#     so the same script was a 59s video against a 38-52s window;
#   * sentence lengths vary hard, from three words to twenty. An even rhythm
#     is the clearest AI tell there is, and the QC scorecard warns on it.
SAMPLE_BEATS = [
    ("hook",
     "पाँच सौ कंकाल, एक जमी हुई झील, और एक भी पक्का जवाब नहीं।",
     "500 kankaal. Ek jami hui jheel. Ek bhi pakka jawaab nahi.",
     "500 KANKAAL",
     "frozen high-altitude lake at dawn, scattered pale bones under clear "
     "ice, himalayan peaks behind, mist"),
    ("setup",
     "उत्तराखंड। सोलह हज़ार फुट।",
     "Uttarakhand. 16,000 foot.",
     None,
     "vast himalayan basin, tiny glacial lake far below, scale of emptiness, "
     "cold blue light"),
    ("setup",
     "उन्नीस सौ बयालीस में एक फ़ॉरेस्ट रेंजर ने इन्हें पहली बार देखा।",
     "1942 mein ek forest ranger ne inhe pehli baar dekha.",
     None,
     "1940s indian forest ranger silhouette at a lake edge, oil lamp, "
     "archival grain, back to camera"),
    ("escalation",
     "पहला अंदाज़ा था — जापानी सैनिक।",
     "Pehla andaaza tha — Japanese sainik.",
     None,
     "wartime era rumour, faded map of the himalayas, pins and string, "
     "dim lamplight on paper"),
    ("escalation",
     "अंदाज़ा ग़लत निकला।",
     "Andaaza galat nikla.",
     None,
     "discarded papers on a desk, a single lamp, cold night through a "
     "window, abandoned investigation"),
    ("reveal",
     "कार्बन डेटिंग ने बताया — ये हड्डियाँ आठ सौ साल पुरानी हैं।",
     "Carbon dating ne bataya — ye haddiyan 800 saal purani hain.",
     "800 SAAL PURANE",
     "laboratory bone sample under cold clinical light, calipers, "
     "scientific instruments, sterile shadows"),
    ("reveal",
     "सबकी खोपड़ी पर चोट एक ही जगह। ऊपर से।",
     "Sabki khopdi par chot ek hi jagah. Upar se.",
     "UPAR SE",
     "hailstorm over a mountain lake at night, enormous hailstones frozen "
     "mid-air, violent sky"),
    ("twist",
     "लेकिन दो हज़ार उन्नीस की डीएनए जाँच ने वो कहानी तोड़ दी। सारे कंकाल "
     "एक जगह के नहीं थे।",
     "Lekin 2019 ki DNA jaanch ne wo kahaani tod di. Saare kankaal ek "
     "jagah ke nahi the.",
     None,
     "dna sequencing visualisation on a dark screen, cold green traces, "
     "researcher shadow"),
    ("cliffhanger",
     "कुछ लोग भूमध्य सागर के थे।",
     "Kuch log Bhumadhya Saagar ke the.",
     "GREECE SE?",
     "ancient mediterranean traveller's worn sandals on himalayan snow, "
     "impossible juxtaposition, cold dusk"),
    ("cliffhanger",
     "वो हिमालय की इस झील तक पहुँचे कैसे।",
     "Wo Himalaya ki is jheel tak pahunche kaise.",
     None,
     "a narrow frozen mountain pass at night, faint tracks in snow leading "
     "upward, no figures"),
    ("cta",
     "रिपोर्ट कहती है तीर्थयात्री। डीएनए कहता है कुछ और।",
     "Report kehti hai teerthyatri. DNA kehta hai kuch aur.",
     None,
     "two conflicting documents side by side on dark wood, one official one "
     "scientific, harsh single light"),
    ("cta",
     "और आज तक कोई नहीं जानता, उस रात हुआ क्या था।",
     "Aur aaj tak koi nahi jaanta, us raat hua kya tha.",
     None,
     "empty frozen lake at last light, single set of footprints leading in "
     "and not out, silence"),
]


MOTIONS = ["zoom_in", "move_left", "zoom_out", "move_right"]
TRANSITIONS = ["fade", "slide_left", "fade", "zoom", "fade", "blur",
               "fade", "slide_right", "fade", "fade", "zoom", "fade"]


def _hooks() -> list[dict]:
    raw = [
        ("question", "पाँच सौ कंकाल एक झील में। कैसे?",
         "500 kankaal ek jheel mein. Kaise?"),
        ("number", "आठ सौ साल। पाँच सौ लाशें। शून्य जवाब।",
         "800 saal. 500 laashein. Zero jawaab."),
        ("contradiction",
         "रिपोर्ट कहती है तीर्थयात्री। डीएनए कहता है ग्रीस।",
         "Report kehti hai teerthyatri. DNA kehta hai Greece."),
        ("claim", "भारत की सबसे डरावनी झील का नाम है रूपकुंड।",
         "Bharat ki sabse darawni jheel ka naam hai Roopkund."),
        ("threat", "जो इस झील पर गया, वो लौटा नहीं।",
         "Jo is jheel par gaya, wo lauta nahi."),
    ]
    return [{"variant_id": f"h{i + 1}", "style": style,
             "voice_text": voice, "caption_text": caption, "seconds": 3.0}
            for i, (style, voice, caption) in enumerate(raw)]


def _script() -> dict:
    beats = []
    for index, (role, voice, caption, punch, prompt) in enumerate(
            SAMPLE_BEATS):
        beats.append({
            "beat_id": f"b{index + 1}",
            "role": role,
            "voice_text": voice,
            "caption_text": caption,
            "on_screen_text": punch,
            "target_seconds": 4.4,
            "visual_prompt": prompt,
            "motion": MOTIONS[index % len(MOTIONS)],
            "transition": TRANSITIONS[index % len(TRANSITIONS)],
        })
    return {"total_seconds": 45.0, "chosen_hook": "h1", "beats": beats}


PAYLOADS: dict[str, dict] = {
    "research": {
        "claims": [
            {"beat_id": "b6", "text": "Roopkund ke kankaal ~800 saal purane",
             "source_url": "https://www.nature.com/articles/s41467-019-11357-8",
             "confidence": "high"},
            {"beat_id": "b7",
             "text": "Khopdi par upar se lagi chot ke nishaan",
             "source_url": "https://en.wikipedia.org/wiki/Roopkund",
             "confidence": "medium"},
            {"beat_id": "b9",
             "text": "Kuch genomes Mediterranean ancestry dikhate hain",
             "source_url": "https://www.nature.com/articles/s41467-019-11357-8",
             "confidence": "high"},
        ],
        "searched_queries": ["roopkund skeletons dna study",
                             "roopkund lake carbon dating"],
        # entities are filled per-topic at call time; see _entities_for.
    },
    "hooks": {"hooks": _hooks()},
    "script": {"script": _script()},
    "metadata": {
        "yt_title": "Roopkund: 500 Kankaal Aur Ek Bhi Jawaab Nahi",
        "yt_description": ("Uttarakhand ki Roopkund jheel mein 500 se zyada "
                           "kankaal hain. 2019 ki DNA study ne inhe Greece "
                           "tak jodha. Sources description mein."),
        "ig_caption": ("500 kankaal, ek jheel, 800 saal. DNA report ne jo "
                       "bataya wo koi expect nahi kar raha tha."),
        "pinned_comment": ("Official report kehti hai ye teerthyatri the jo "
                           "oley se mare. 2019 ki DNA study kehti hai kuch "
                           "log Bhumadhya Saagar se the. Dono sach nahi ho "
                           "sakte. Tum kispe bharosa karoge?"),
        "hashtags": ["#roopkund", "#rahasya", "#unsolvedmystery",
                     "#uttarakhand", "#indianhistory"],
        "thumbnail_prompt": ("pale bones under clear ice, himalayan peaks, "
                             "cold moonlight, ominous"),
    },
}


def _entities_for(prompt: str) -> list[str]:
    """Derive plausible entities from whatever topic was actually typed.

    Crude on purpose — this stands in for a model. It only has to be
    *distinct per topic*, so the dedup gate behaves the way it would with a
    real research call instead of locking up after one video.
    """
    marker = "TOPIC:"
    line = ""
    if marker in prompt:
        line = prompt.split(marker, 1)[1].splitlines()[0]
    words = [w.strip(".,!?\"'()") for w in line.split()]
    # Hinglish question words and particles carry no identity.
    skip = {
        # particles and question words
        "ka", "ki", "ke", "ko", "kyu", "kyun", "hai", "hain", "mein", "me",
        "par", "se", "aur", "ek", "kaun", "kya", "kaise", "nahi", "kaha",
        "kahan", "kab", "the", "tha", "thi", "wala", "wali", "bhi", "koi",
        "of", "in", "a", "is", "why", "what", "how", "who", "where",
        # verbs and adverbs that show up in mystery topics
        "jana", "jaana", "mana", "hua", "huaa", "gaya", "mile", "milta",
        "andar", "bahar", "raat", "din", "saal", "purane", "purana",
        "khaali", "khali", "band", "bandh", "gayab", "lapata",
        # generic nouns: these are categories, not subjects. Letting them
        # through would make one fort block every other fort.
        "fort", "killa", "qila", "jheel", "lake", "gaon", "gaanv", "village",
        "mandir", "temple", "mahal", "palace", "haveli", "kila", "kankaal",
        "kankal", "skeleton", "rahasya", "mystery", "sach", "kahani",
        "kahaani", "story", "log", "logon", "aadmi", "insaan",
    }
    picked = [w for w in words if len(w) > 3 and w.lower() not in skip]
    return [w.title() for w in picked[:3]] or ["Unknown Subject"]


class FakeOmniRoute:
    """Same surface as OmniRouteClient, no network."""

    def __init__(self, *, usd_per_call: float = 0.0004,
                 provider: str = "fake", fail_images: bool = True):
        self.usd_per_call = usd_per_call
        self.provider = provider
        self.fail_images = fail_images
        self.calls: list[CostRecord] = []
        self.total_usd = 0.0
        self.stages: list[str] = []
        self.default_chat_model = "fake/model"
        self.default_embed_model = "fake/embed"
        self.default_image_model = "fake/image"

    def _cost(self) -> CostRecord:
        record = CostRecord(usd=self.usd_per_call, provider=self.provider,
                            model="fake/model", latency_ms=120)
        self.calls.append(record)
        self.total_usd += record.usd
        return record

    def chat(self, messages, *, model=None, want_json=False,
             temperature=0.85, max_tokens=4096) -> ChatResult:
        blob = " ".join(
            m.get("content", "") for m in messages if isinstance(m, dict))
        stage = "metadata"
        for name in ("research", "hooks", "script", "metadata"):
            if f"STAGE:{name}" in blob:
                stage = name
                break
        self.stages.append(stage)

        data = dict(PAYLOADS[stage])
        if stage == "research":
            # The sample script is fixed, and the banner says so. The entity
            # list must NOT be: it feeds the 45-day cooldown layer, and
            # returning the same three names for every topic wrote them
            # against the first approved plan and then blocked every topic
            # after it. The fake has to vary where the gates read from it.
            data["entities"] = _entities_for(blob)
        return ChatResult(text="", cost=self._cost(), data=data, raw={})

    def image(self, prompt, *, model=None, size="1024x1792", n=1):
        self._cost()
        if self.fail_images:
            # Mirrors reality: a gateway with no image provider 400s here, and
            # the media stage must fall back rather than abort the video.
            raise RuntimeError("no image provider configured")
        return [b"\x89PNG\r\n\x1a\n"]

    def embed(self, texts, *, model=None):
        self._cost()
        return [[0.01 * (i + 1) for i in range(8)] for _ in texts]

    def moderate(self, text, *, model="omni-moderation-latest"):
        return ModerationResult(flagged=False, flags=[], cost=self._cost())

    def ping(self, timeout: float = 2.5) -> bool:
        return True

    def health(self, timeout: float = 25.0) -> dict:
        return {"state": "ready", "models": 0, "detail": "fake client"}

    def close(self) -> None:
        pass
