"""Merge 'Équipements PBM' + 'Autres appareils' into one clean list (no 'AUTRE ...' left)."""
import re, unicodedata

def key(s):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode().lower())

# free-text device -> canonical name(s); matched on key() substring, first hit wins
CANON = [
    ('caremin', ['CAREMIN 650']), ('carmin650', ['CAREMIN 650']),
    ('casquemilta', ['CASQUE MILTA']), ('miltapad', ['MILTA PAD']), ('padmilta', ['MILTA PAD']),
    ('rejulight', ['REJULIGHT XP']),
    ('vielightneuroduo', ['VIELIGHT NEURO DUO']), ('veilightneuroduo', ['VIELIGHT NEURO DUO']),
    ('vielightduo', ['VIELIGHT NEURO DUO']), ('vielightvagus', ['VIELIGHT VAGUS']), ('veilightvagus', ['VIELIGHT VAGUS']),
    ('casquenoance', ['CASQUE NOOANCE']), ('domenooance', ['DÔME NOOANCE']),
    ('casqueneuraled', ['CASQUE NEURALED']), ('casquepbm', ['CASQUE PBM']),
    ('lightforce', ['LIGHTFORCE']), ('lighforce', ['LIGHTFORCE']),
    ('qwave', ['QWAVE']), ('medisol', ['MEDISOL']),
    ('asalaserm8etmi', ['ASALASER M8', 'ASALASER M-HI']),
    ('asalaserm8asalasermhi', ['ASALASER M8', 'ASALASER M-HI']),
    ('rjlaserpen', ['RJ LASERPEN M']),
    ('cabinecorpsentier', ['CABINE CORPS ENTIER MN6 MERICAN']),
    ('ledlightbioledtherapy', ['LED LIGHT BIOLEDTHERAPY']),
    ('kenodome', ['KENO DOME (VERT / INFRAROUGE)']),
    ('auroraglobalmed', ['AURORA GLOBALMED AESTHETIC']),
    ('spectrometre', ['SPECTROMÈTRES THORLABS ET HOPOOCOLOR']),
    ('concentrateursluminescents', []),  # description of research work, not a device
    ('mostleds', ['MOSTLEDS']), ('laser tl150'.replace(' ', ''), ['LASER TL150']),
    ('casque', ['CASQUE PBM']),
]
HAND_LASERS = {'LIGHTFORCE', 'ASALASER M8', 'ASALASER MI', 'ASALASER M-HI', 'RJ LASERPEN M', 'LASER TL150', 'MLS'}
WHOLE_BODY = {'CABINE CORPS ENTIER MN6 MERICAN'}

def other_devices(text):
    out = []
    text = re.sub(r'MN6\s*\+\s*MERICAN', 'MN6 MERICAN', text or '', flags=re.I)
    for part in re.split(r'\n|,(?![^()]*\))|\s\+\s', text or ''):
        p = re.sub(r'^\s*\d+\s+|\s+x\s*\d+\s*$', '', part.strip(), flags=re.I).strip(' .')
        if not p:
            continue
        k = key(p)
        hit = next((names for frag, names in CANON if frag in k), None)
        out += hit if hit is not None else [p.upper()]
    return out

def merge(pbm, autres):
    others = other_devices(autres)
    result = []
    for item in [x.strip() for x in (pbm or '').split(',') if x.strip()]:
        u = item.upper()
        if u == 'AUTRE(S)':
            continue  # replaced by the 'Autres appareils' column
        if u.startswith('AUTRE LASER'):
            if not any(o in HAND_LASERS for o in others):
                result.append('LASER À MAIN')
        elif u == 'AUTRE CORPS ENTIER':
            if not any(o in WHOLE_BODY for o in others):
                result.append('CORPS ENTIER')
        elif u.startswith('AUTRE '):
            result.append(u[6:])
        else:
            result.append(u)
    for o in others:
        result.append(o)
    seen, clean = set(), []
    for x in result:
        if x not in seen:
            seen.add(x); clean.append(x)
    return clean
