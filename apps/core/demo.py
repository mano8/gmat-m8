"""Data for ``manage seed_demo``: the A/B/C organization of plan §3.2, people, projects,
a catalog of about 200 materials and five suppliers. Invented names throughout.

The catalog is generated from families (a name pattern, a unit and the values it varies
over), so it stays readable and the references are stable: ``seed_demo`` runs again without
changing anything.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product

from apps.access.roles import Role

DEMO_DOMAIN = "demo.example"


@dataclass(frozen=True)
class DemoUser:
    key: str  # the local part of the address
    name: str
    employer: str  # company code
    memberships: tuple[tuple[Role, str], ...]  # (role, "A-MAD" branch or "C" company code)

    @property
    def email(self) -> str:
        return f"{self.key}@{DEMO_DOMAIN}"


COMPANIES = (
    ("A", "Instalaciones Alfa SA", "STANDARD"),
    ("B", "UTE Alfa-Cienfuegos", "JOINT_VENTURE"),
    ("C", "Cienfuegos Electricidad SL", "STANDARD"),
)
JOINT_VENTURE_MEMBERS = (("B", "A"), ("B", "C"))
# (company, code, name, city, managed by "A-MAD" or None)
BRANCHES = (
    ("A", "MAD", "Alfa Madrid", "Madrid", None),
    ("A", "SEV", "Alfa Sevilla", "Sevilla", None),
    ("B", "MAD", "UTE Madrid", "Madrid", "A-MAD"),
)
WAREHOUSES = (("A-MAD", "Almacén Madrid"), ("A-SEV", "Almacén Sevilla"), ("B-MAD", "Almacén UTE"))

USERS = (
    DemoUser("mm-mad", "Marta Molina", "A", ((Role.MATERIAL_MANAGER, "A-MAD"),)),
    DemoUser("mm-sev", "Mateo Soler", "A", ((Role.MATERIAL_MANAGER, "A-SEV"),)),
    DemoUser("wo-mad", "Waldo Ortega", "A", ((Role.WAREHOUSE_OPERATOR, "A-MAD"),)),
    DemoUser("tech-mad", "Tomás Herrero", "A", ((Role.TECHNICIAN, "A-MAD"),)),
    DemoUser("site-manager", "Sara Gil", "A", ((Role.SITE_MANAGER, "A-MAD"),)),
    DemoUser("partner", "Pablo Cienfuegos", "C", ((Role.PARTNER_REQUESTER, "C"),)),
    DemoUser(
        "mixed",
        "Manuel Mixto",
        "A",
        ((Role.MATERIAL_MANAGER, "A-SEV"), (Role.TECHNICIAN, "A-MAD")),
    ),
)

# (code, name, branch, procurement branch, status, client)
PROJECTS = (
    ("HOSP-X", "Hospital X", "B-MAD", "A-MAD", "ACTIVE", "Servicio de Salud"),
    ("COL-MAD", "Colegio San Isidro", "A-MAD", "A-MAD", "ACTIVE", "Ayuntamiento"),
    ("NAVE-SEV", "Nave logística Sevilla", "A-SEV", "A-SEV", "ACTIVE", "Logística Sur SA"),
    (
        "OFI-MAD",
        "Oficinas Castellana (en pausa)",
        "A-MAD",
        "A-MAD",
        "ON_HOLD",
        "Inmobiliaria Norte",
    ),
)
# (user key, project code, assignment kind)
ASSIGNMENTS = (("site-manager", "HOSP-X", "MANAGER"), ("partner", "HOSP-X", "PARTNER"))

# (name, tax id, contact, email, phone, address)
SUPPLIERS = (
    (
        "Suministros Eléctricos Levante SL",
        "B46000001",
        "Elena Ruiz",
        "pedidos@levante.example",
        "+34 960 000 001",
        "Pol. Ind. Fuente del Jarro, Paterna",
    ),
    (
        "Distribuciones Cobre Norte SA",
        "A48000002",
        "Iker Etxeberria",
        "ventas@cobrenorte.example",
        "+34 944 000 002",
        "Parque Empresarial Zamudio, Bilbao",
    ),
    (
        "Iluminación Técnica Ibérica SL",
        "B28000003",
        "Lucía Prado",
        "comercial@iluminacion.example",
        "+34 910 000 003",
        "Av. de la Industria 12, Alcobendas",
    ),
    (
        "Ferretería Industrial del Sur SL",
        "B41000004",
        "Rafael Montes",
        "info@ferresur.example",
        "+34 955 000 004",
        "Pol. Ind. La Negrilla, Sevilla",
    ),
    (
        "Canalizaciones del Tajo SL",
        "B45000005",
        "Carmen Vidal",
        "ventas@canatajo.example",
        "+34 925 000 005",
        "Pol. Ind. Santa María de Benquerencia, Toledo",
    ),
)


@dataclass(frozen=True)
class DemoMaterial:
    reference: str
    name: str
    unit: str
    category: str  # "Parent > Child"
    manufacturer: str
    is_stock_item: bool


def _decimal(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def materials() -> Iterator[DemoMaterial]:
    """About 200 materials, in a fixed order."""
    colors = (
        ("AZ", "azul"),
        ("MA", "marrón"),
        ("NE", "negro"),
        ("GR", "gris"),
        ("VA", "verde-amarillo"),
    )
    for section, (color_code, color_name) in product((1.5, 2.5, 4, 6, 10, 16), colors):
        yield DemoMaterial(
            f"CAB-H07VK-{_decimal(section)}-{color_code}",
            f"Cable H07V-K 1x{_decimal(section)} mm² {color_name}",
            "M",
            "Cables y conductores > Unipolares",
            "Prysmian",
            True,
        )
    for cores, area in ((3, 1.5), (3, 2.5), (3, 4), (5, 1.5), (5, 2.5), (5, 4), (5, 6), (5, 10)):
        yield DemoMaterial(
            f"CAB-RVK-{cores}X{_decimal(area)}",
            f"Manguera RV-K 0,6/1 kV {cores}x{_decimal(area)} mm²",
            "M",
            "Cables y conductores > Mangueras",
            "General Cable",
            False,
        )
    for poles, amps in product(("1P", "1P+N", "2P", "3P", "4P"), (10, 16, 20, 25, 32, 40, 63)):
        yield DemoMaterial(
            f"MAG-{poles.replace('+', '')}-{amps}C",
            f"Interruptor magnetotérmico {poles} {amps}A curva C",
            "UD",
            "Protección > Magnetotérmicos",
            "Schneider Electric",
            amps <= 32,
        )
    for poles, amps, sensitivity in product(("2P", "4P"), (25, 40, 63), (30, 300)):
        yield DemoMaterial(
            f"DIF-{poles}-{amps}-{sensitivity}",
            f"Interruptor diferencial {poles} {amps}A {sensitivity}mA",
            "UD",
            "Protección > Diferenciales",
            "Schneider Electric",
            sensitivity == 30,
        )
    for diameter in (16, 20, 25, 32, 40, 50):
        yield DemoMaterial(
            f"TUB-COR-M{diameter}",
            f"Tubo corrugado reforzado M{diameter}",
            "ROLLO",
            "Canalización > Tubos",
            "Aiscan",
            diameter <= 25,
        )
        yield DemoMaterial(
            f"TUB-RIG-M{diameter}",
            f"Tubo rígido PVC M{diameter} (barra de 3 m)",
            "UD",
            "Canalización > Tubos",
            "Aiscan",
            False,
        )
    for width, height in ((60, 100), (100, 100), (200, 100), (300, 100)):
        yield DemoMaterial(
            f"BAN-REJ-{width}X{height}",
            f"Bandeja rejilla {width}x{height} mm (tramo de 3 m)",
            "UD",
            "Canalización > Bandejas",
            "Unex",
            False,
        )
    for side, protection in product((80, 100, 150, 200), (54, 65)):
        yield DemoMaterial(
            f"CAJ-REG-{side}-IP{protection}",
            f"Caja de registro {side}x{side} mm IP{protection}",
            "UD",
            "Cajas y envolventes > Cajas de registro",
            "Gewiss",
            side <= 100,
        )
    for series, finish in product(
        ("Serie Plana", "Serie Clásica", "Serie Industrial"), ("blanco", "antracita")
    ):
        slug = f"{series.split()[1][:3].upper()}-{finish[:3].upper()}"
        yield DemoMaterial(
            f"MEC-ENCH-{slug}",
            f"Base de enchufe 16A 2P+TT {series} {finish}",
            "UD",
            "Mecanismos > Enchufes",
            "Simon",
            True,
        )
        yield DemoMaterial(
            f"MEC-INT-{slug}",
            f"Interruptor unipolar 10A {series} {finish}",
            "UD",
            "Mecanismos > Interruptores",
            "Simon",
            True,
        )
    for kind, watts, kelvin in product(
        ("Downlight", "Panel 60x60", "Regleta estanca", "Proyector"), (18, 36, 50), (4000, 6500)
    ):
        slug = kind.split()[0][:4].upper()
        yield DemoMaterial(
            f"LUM-{slug}-{watts}-{kelvin}",
            f"Luminaria LED {kind} {watts}W {kelvin}K",
            "UD",
            "Iluminación > Luminarias LED",
            "Philips",
            False,
        )
    for ways in (2, 3, 5, 12):
        yield DemoMaterial(
            f"CON-REG-{ways}",
            f"Regleta de conexión {ways} polos 2,5 mm²",
            "UD",
            "Conexión > Regletas y bornes",
            "Wago",
            True,
        )
        yield DemoMaterial(
            f"CON-BOR-{ways}",
            f"Borne de empalme {ways} hilos",
            "BOLSA",
            "Conexión > Regletas y bornes",
            "Wago",
            True,
        )
    for length in (100, 150, 200, 300):
        yield DemoMaterial(
            f"FIJ-BRI-{length}",
            f"Brida de nylon {length} mm (bolsa de 100)",
            "BOLSA",
            "Fijación > Bridas y tacos",
            "Fischer",
            True,
        )
    for diameter in (6, 8, 10):
        yield DemoMaterial(
            f"FIJ-TAC-{diameter}",
            f"Taco de nylon {diameter} mm (caja de 100)",
            "CAJA",
            "Fijación > Bridas y tacos",
            "Fischer",
            True,
        )
    for poles, amps, coil in product(("3P", "4P"), (9, 12, 18, 25, 32), ("24V", "230V")):
        yield DemoMaterial(
            f"CNT-{poles}-{amps}-{coil}",
            f"Contactor {poles} {amps}A bobina {coil}",
            "UD",
            "Cuadros y control > Contactores",
            "Schneider Electric",
            False,
        )
    fuses = (
        ("10x38", (6, 10, 16, 20)),
        ("14x51", (16, 25, 32)),
        ("22x58", (40, 50, 63, 80, 100)),
    )
    for size, amps in ((size, amps) for size, ratings in fuses for amps in ratings):
        yield DemoMaterial(
            f"FUS-{size}-{amps}",
            f"Fusible cilíndrico gG {size} {amps}A",
            "UD",
            "Protección > Fusibles",
            "Ferraz",
            True,
        )
    for kind, lens in product(("Piloto", "Pulsador"), ("rojo", "verde", "amarillo", "azul")):
        yield DemoMaterial(
            f"CTL-{kind[:3].upper()}-{lens[:3].upper()}",
            f"{kind} luminoso 22 mm {lens}" if kind == "Piloto" else f"Pulsador 22 mm {lens}",
            "UD",
            "Cuadros y control > Mando y señalización",
            "Schneider Electric",
            False,
        )
    yield DemoMaterial(
        "CON-CINTA-AIS", "Cinta aislante PVC 19 mm negra", "ROLLO", "Consumibles", "3M", True
    )
    yield DemoMaterial(
        "CON-PEGA-PVC", "Adhesivo para PVC 250 ml", "BOTE", "Consumibles", "Fischer", True
    )
    yield DemoMaterial(
        "CON-GRASA", "Pasta lubricante para cables", "KG", "Consumibles", "Ega Master", True
    )
    yield DemoMaterial("CON-LIMPIA", "Limpiador de contactos", "L", "Consumibles", "3M", True)
