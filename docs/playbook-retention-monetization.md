# Retention aur Monetization Playbook

**Track:** Hinglish/Hindi-first, India audience
**Primary platform:** Instagram Reels · **Secondary:** YouTube Shorts
**Date:** 2026-09-17

Ye document do sawaalon ka jawaab hai: video ko **dekha** kaise jaaye, aur usse
**paisa** kaise aaye. Numbers tere `youtube-automation-feasibility-report.md`
se hain.

---

## 1. Pehle economics, kyunki wahi baaki sab decide karta hai

| Stream | Kab shuru hota hai | Iss track pe realistic |
|---|---|---|
| **Instagram brand deals** | ~10k followers, achhi engagement | **Sabse bada** |
| **Affiliate** | Din 1. Koi threshold nahi | Achha, compounding |
| YouTube Shorts AdSense | YPP + **10M views/90 din** | ₹5–30 RPM. **Practically zero** |
| Digital product (tera pipeline) | ~10k followers | Highest margin |

Shorts payout floor pe dhyaan de: **10M qualified Shorts views trailing
90 days** ke bina Shorts pool se **kuch nahi** milta. 1 Feb 2027 se naye YPP
applicants ko Shorts route pe 20M views/90d chahiye.

**Toh design decision ye hai:** engine views ke liye optimize nahi karta.
Wo **saves, shares aur comments** ke liye karta hai — kyunki brand deals inhi
pe price hote hain. Ek video jise 40k log dekhte hain aur 3k save karte hain,
us video se zyada valuable hai jise 200k dekhte hain aur 400 save karte hain.

Jo log fail karte hain wo AdSense ka intezaar karte hain. Affiliate aur brand
deals ko YPP ki zaroorat nahi — video 1 se shuru kar.

---

## 2. Pehle 3 second

Yahan 80% faisla hota hai. Reels muted autoplay karti hai, toh **pehla frame
aur pehla text** kaam karte hain, voice nahi.

### Paanch hook styles (engine paanchon generate karta hai, tu chunta hai)

| Style | Kab use kare | Hinglish example |
|---|---|---|
| **contradiction** | Jab do sources takraate hain. **Sabse strong.** | "Report kehti hai teerthyatri. DNA kehta hai Greece." |
| **number** | Jab aankda khud sawaal khada karta hai | "800 saal. 500 laashein. Zero jawaab." |
| **question** | Jab sawaal ka aasaan jawaab nahi hai | "500 kankaal ek jheel mein. Kaise?" |
| **claim** | Flat, confident, verifiable | "Bharat ki sabse darawni jheel Uttarakhand mein hai." |
| **threat** | Implied danger, **bina fact banaye** | "Jo is jheel par gaya, wo lauta nahi." |

**Default choice: contradiction.** Ye curiosity gap sabse tight banata hai aur
comments me do side automatically ban jaate hain — jo directly monetization
metric hai.

### Jo kabhi nahi karna

Ye phrases QC me **hard fail** hain, engine inhe reject karta hai:

- "aaj hum baat karenge" · "kya aap jaante hain" · "doston"
- "chaliye shuru karte hain" · "aap ko jaan kar hairani hogi"
- Koi bhi channel intro, logo, ya greeting
- Pehle 3 second me apna naam

Reason sirf taste nahi hai. Ye exactly wo phrasing hai jo templated channels
ko ek saath cluster karti hai YouTube ke pattern detection me.

---

## 3. 45 second ka beat map

Engine 9–13 beats generate karta hai. Structure ye hai:

```
0-3s    hook          specific unresolved cheez. Scroll rok.
3-11s   setup         kahan, kab, kisne dekha. Do beats.
11-19s  escalation    pehla explanation — aur wo kyun fail hua.
19-27s  reveal        evidence ne actually kya dikhaya.
27-31s  twist         "LEKIN ..." <- 60% mark pe pattern interrupt
31-41s  cliffhanger   jo hissa aaj bhi unexplained hai.
41-45s  loop close    aakhri line hook ko reframe karti hai.
```

**Teen cheezein jo isme non-obvious hain:**

**Twist "लेकिन" se shuru hona chahiye.** 60% mark pe drop-off sabse zyada
hota hai. Ek reversal wahan attention reset kar deta hai. Engine ka script
prompt ise force karta hai.

**Loop close summary nahi hai.** Aakhri line hook ko naye matlab deti hai,
taaki dobara dekhna *zaroori* lage. "To ye thi kahaani" loop kill karta hai.
Instagram replay ko watch time ginta hai — 45s ka video jo 1.3x replay hota
hai, 58s effective watch time deta hai.

**Har 2.5–4 second me visual change.** QC isko warn karta hai
(`visual_change_rate`). 10 beats × 4.5s = 4.5s per visual, jo thoda slow hai.
12 beats better hai.

### Sentence rhythm — asli AI-tell

Do teen shabd ka beat, uske baad baarah shabd ka beat. Flat even rhythm sabse
saaf AI signature hai, aur audience ise pehchanti hai bina samjhe ki kyun.

QC me `sentence_variance` warn hai — stdev 5.5 se upar chahiye. Verified run
me 3.2 aaya, jo bahut flat hai. **Approve karte waqt do-teen beats ko chhota
kar de.** Ye wo 30 second hai jo sabse zyada farak karta hai.

---

## 4. Pinned comment — sabse high-leverage 30 seconds

Brand deals comments pe price hote hain. Ek generic "aapka kya khayal hai?"
generic engagement laata hai, jiski value zero hai.

**Formula:** Position A batao. Position B batao. Poocho kispe bharosa hai.

> Official report kehti hai ye teerthyatri the jo oley se mare. 2019 ki DNA
> study kehti hai kuch log Bhumadhya Saagar se the. Dono sach nahi ho sakte.
> Tum kispe bharosa karoge?

Ye kaam kyun karta hai: dono side defensible hain, toh log **ek side chunte
hain aur usko argue karte hain**. Argument dusre logon ko reply karwata hai.
Ek thread 50 comments ka ho jaata hai.

**Publish karne ke *turant baad* post kar aur pin kar.** Pehle 30 minute ka
comment velocity distribution decide karta hai. Panel me copy button hai.

Kabhi bhi like/follow/subscribe nahi maangna — na script me, na comment me.
Script prompt ise ban karta hai. Wo attention pinned comment se cheenta hai.

---

## 5. Hashtags aur caption

**5–8 hashtags, size mix:**
- 2 broad — `#rahasya` `#unsolvedmystery`
- 3–4 topic-specific — `#roopkund` `#uttarakhand` `#himalaya`
- 1–2 place/period — `#indianhistory`

`#fyp`, `#viral`, `#explore` **use nahi karna.** Ye reach nahi dete aur
spam-signal dete hain.

**Caption sound-off kaam kare.** Reels ka pehla caption line feed me dikhta
hai. Open question pe khatam kar, hashtags caption ke andar nahi — alag
line me.

---

## 6. Cadence — jahan log channel maarte hain

Ye sabse counter-intuitive section hai, toh dhyaan se.

**2–3 videos per din se zyada nahi. Aur har din nahi.**

Jan 2026 me YouTube ne 16 channels **terminate** kiye — 4.7B lifetime views,
35M subscribers. Demonetise nahi, delete. Pattern jo pakda gaya:
synthetic narration + stock footage + **high upload cadence**. Detection ab
video-level nahi, **channel-level** hai.

Toh:
- **2 video/din cap.** Engine isse zyada fast chal sakta hai. Mat chalao.
- **Har 45 din me ek entity repeat nahi.** Engine enforce karta hai
  (cooldown layer).
- **Semantic similarity 0.88 se neeche.** Engine hard-fail karta hai.
- **Ek hafte me 1 din gap.** Machine-cadence sabse saaf signal hai.

Ye teeno gates efficiency features nahi hain. Ye policy defence hain.

### Synthetic media disclosure

**Har upload pe set karna hai.** QC isko hard-fail karta hai, aur publish
payload me `containsSyntheticMedia: true` already hai. YouTube Studio me
toggle bhi confirm kar — Jan 2026 se ye compliance step hai, optional nahi.

Chhupane ki koshish karne se jo risk hai wo disclose karne ke reach-loss se
bahut bada hai.

---

## 7. Copyright — jo tu galat cheez se dar raha hoga

Risk stock footage me nahi hai. Risk **claims** me hai.

- **AI-generated visuals** — tere hain, koi third-party claim nahi.
- **AI voice (edge-tts)** — commercial use ke liye Microsoft ki terms check
  kar le. Ye ek real open item hai.
- **Facts** — copyrightable nahi. Batao, aur source do.
- **Text jo kisi article se copy hua** — **yahan problem hai.** Engine
  claims store karta hai source URL ke saath, phrasing nahi. Wahi sahi hai.

`assets` table me provider, licence, source URL aur checksum per asset store
hota hai. Agar kabhi claim aaya, **wo table tera defence hai.** Use delete
mat karna.

---

## 8. Brand deal readiness

Approach karne se pehle ye numbers chahiye. Followers sabse kam important hai.

| Metric | Target | Kyun |
|---|---|---|
| **Saves per 1k views** | 25+ | Sabse strong buying signal |
| **Shares per 1k views** | 15+ | Organic reach ka engine |
| **Comments per 1k views** | 8+ | Pinned comment ka kaam |
| Avg watch % | 65%+ | 45s pe ~29s |
| Follower growth | 8%+ / month | Trajectory, absolute number nahi |
| Consistency | 8+ hafte | Bharosa |

**10k followers pe achhe saves ke saath**, ek Hindi mystery page India me
per-post ₹8,000–25,000 charge kar sakta hai. **50k pe** ₹40,000–1,00,000.
Ye views se nahi, engagement rate aur niche fit se aata hai.

Brand fit iss niche me: audiobook/story apps, travel gear, history courses,
true-crime podcasts, sleep/focus apps. **Finance aur health se door rehna** —
claims risk bahut zyada hai.

### Affiliate jo iss niche me actually convert karta hai

1. **Kindle/audiobook** — mystery books. Natural fit, har video me.
2. **History/documentary courses**
3. **Travel gear** — jab video kisi reachable jagah ke baare me ho
4. **Apna pipeline** — report §1 ka finding: ye Year 1 me channel se zyada
   monetizable hai. Agencies ₹40k–1.5L/month pay karti hain.

---

## 9. Pehle 30 din

| Hafta | Kaam |
|---|---|
| 1 | Ek provider key add kar (`docs/omniroute-setup.md`). 5 video **manually** review kar. Voice ko kaan se sun — dark lag raha hai ya cheerful? |
| 2 | 10 video. Hook style A/B kar. Instagram Business account + FB Page link kar. |
| 3 | 20 video ho gaye. Saves/1k dekh. Top 3 aur bottom 3 ka hook style compare kar. |
| 4 | Jo hook style jeeta usko default bana. Affiliate links add kar. Pehla brand outreach 2k followers pe hi kar de — "growing page" pitch chalti hai. |

**Week 1 me ek cheez jo skip mat karna:** 5 video khud manually review kar,
render se pehle. Tab tu seekhega ki script prompt me kya galat hai. Uske baad
approval 90 second ka kaam hai.

---

## 10. Jo engine tere liye enforce karta hai

Ye tables reference ke liye hain — panel me dikhte hain.

**Hard fail (human queue me jaata hai, retry nahi hota):**
duration 38–52s · har claim ka source URL · semantic similarity < 0.88 ·
moderation clean · banned phrase zero · 9–13 beats · dono text fields
present · silence gap ≤ 1.2s · caption alignment ≥ 98%

**Warn (dikhta hai, rokta nahi):**
visual change rate ≤ 4s · sentence stdev > 5.5 · title ≤ 100 chars ·
pinned comment debate-shaped

**Automatic nahi hai — jaan-boojh ke:** publishing. Payload panel me banta
hai, upload tu karta hai, video saamne rakh ke. Ye scaling limitation nahi
hai; ye wo ek insaani step hai jo poore setup ko "inauthentic content"
bucket se bahar rakhta hai.
