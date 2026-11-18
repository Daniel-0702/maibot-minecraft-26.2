"""基础几何类型：坐标、方块位置、碰撞箱、朝向。

坐标约定与 Minecraft 保持一致：

* ``Vec3`` 是浮点世界坐标，``y`` 向上；玩家坐标指脚底中心。
* ``BlockPos`` 是整数方块坐标，方块 ``(x, y, z)`` 占据 ``[x, x+1)`` 区间。
* ``yaw``：0 = 正南（+Z），90 = 正西（-X），180 = 正北（-Z），270 = 正东（+X）。
* ``pitch``：正值向下看，范围 ``[-90, 90]``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "Vec3",
    "BlockPos",
    "AABB",
    "Rotation",
    "DIRECTIONS",
    "DIRECTION_VECTORS",
    "wrap_degrees",
    "wrap_radians",
    "look_direction",
    "look_at",
    "horizontal_distance",
]


def wrap_degrees(degrees: float) -> float:
    """把角度规范到 ``[-180, 180)``。"""
    return (degrees + 180.0) % 360.0 - 180.0


def wrap_radians(radians: float) -> float:
    """把弧度规范到 ``[-pi, pi)``。"""
    return (radians + math.pi) % (2.0 * math.pi) - math.pi


@dataclass(frozen=True, slots=True)
class Vec3:
    """三维浮点向量。"""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> "Vec3":
        return Vec3(self.x * scalar, self.y * scalar, self.z * scalar)

    __rmul__ = __mul__

    def __truediv__(self, scalar: float) -> "Vec3":
        return Vec3(self.x / scalar, self.y / scalar, self.z / scalar)

    def __neg__(self) -> "Vec3":
        return Vec3(-self.x, -self.y, -self.z)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def horizontal_length(self) -> float:
        return math.hypot(self.x, self.z)

    def normalized(self) -> "Vec3":
        length = self.length()
        if length < 1e-9:
            return Vec3()
        return self / length

    def distance_to(self, other: "Vec3") -> float:
        return (self - other).length()

    def floor(self) -> "BlockPos":
        return BlockPos(math.floor(self.x), math.floor(self.y), math.floor(self.z))

    def replace(self, *, x: float | None = None, y: float | None = None, z: float | None = None) -> "Vec3":
        return Vec3(self.x if x is None else x, self.y if y is None else y, self.z if z is None else z)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z


ZERO_VEC3 = Vec3()


@dataclass(frozen=True, slots=True, order=True)
class BlockPos:
    """整数方块坐标（方块原点，即最小角）。"""

    x: int = 0
    y: int = 0
    z: int = 0

    def offset(self, dx: int = 0, dy: int = 0, dz: int = 0) -> "BlockPos":
        return BlockPos(self.x + dx, self.y + dy, self.z + dz)

    def above(self, n: int = 1) -> "BlockPos":
        return BlockPos(self.x, self.y + n, self.z)

    def below(self, n: int = 1) -> "BlockPos":
        return BlockPos(self.x, self.y - n, self.z)

    def neighbors(self) -> "tuple[BlockPos, ...]":
        return (
            self.offset(1, 0, 0),
            self.offset(-1, 0, 0),
            self.offset(0, 0, 1),
            self.offset(0, 0, -1),
            self.offset(0, 1, 0),
            self.offset(0, -1, 0),
        )

    @property
    def chunk_x(self) -> int:
        return self.x >> 4

    @property
    def chunk_z(self) -> int:
        return self.z >> 4

    def center(self) -> Vec3:
        return Vec3(self.x + 0.5, self.y + 0.5, self.z + 0.5)

    def to_vec3(self) -> Vec3:
        return Vec3(float(self.x), float(self.y), float(self.z))

    def distance_to(self, other: "BlockPos") -> float:
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2)

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.x, self.y, self.z)

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z


@dataclass(frozen=True, slots=True)
class AABB:
    """轴对齐包围盒。"""

    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def intersects(self, other: "AABB") -> bool:
        return (
            self.min_x < other.max_x
            and self.max_x > other.min_x
            and self.min_y < other.max_y
            and self.max_y > other.min_y
            and self.min_z < other.max_z
            and self.max_z > other.min_z
        )

    def expand(self, dx: float, dy: float, dz: float) -> "AABB":
        return AABB(
            self.min_x - dx,
            self.min_y - dy,
            self.min_z - dz,
            self.max_x + dx,
            self.max_y + dy,
            self.max_z + dz,
        )

    def offset(self, dx: float, dy: float, dz: float) -> "AABB":
        return AABB(
            self.min_x + dx,
            self.min_y + dy,
            self.min_z + dz,
            self.max_x + dx,
            self.max_y + dy,
            self.max_z + dz,
        )

    def offset_vec(self, vector: Vec3) -> "AABB":
        return self.offset(vector.x, vector.y, vector.z)

    def inflate(self, amount: float) -> "AABB":
        return self.expand(amount, amount, amount)

    @property
    def center(self) -> Vec3:
        return Vec3(
            (self.min_x + self.max_x) * 0.5,
            (self.min_y + self.max_y) * 0.5,
            (self.min_z + self.max_z) * 0.5,
        )

    @property
    def size_x(self) -> float:
        return self.max_x - self.min_x

    @property
    def size_y(self) -> float:
        return self.max_y - self.min_y

    @property
    def size_z(self) -> float:
        return self.max_z - self.min_z

    def contains(self, point: Vec3) -> bool:
        return (
            self.min_x <= point.x <= self.max_x
            and self.min_y <= point.y <= self.max_y
            and self.min_z <= point.z <= self.max_z
        )

    @classmethod
    def from_block(cls, x: int, y: int, z: int, box: tuple[float, float, float, float, float, float]) -> "AABB":
        """把方块内的局部碰撞箱（0..1）转换为世界坐标。"""
        x1, y1, z1, x2, y2, z2 = box
        return cls(x + x1, y + y1, z + z1, x + x2, y + y2, z + z2)

    @classmethod
    def from_center(cls, center: Vec3, width: float, height: float) -> "AABB":
        half = width * 0.5
        return cls(
            center.x - half,
            center.y,
            center.z - half,
            center.x + half,
            center.y + height,
            center.z + half,
        )

    @classmethod
    def of_block(cls, pos: BlockPos) -> "AABB":
        return cls(float(pos.x), float(pos.y), float(pos.z), pos.x + 1.0, pos.y + 1.0, pos.z + 1.0)


@dataclass(frozen=True, slots=True)
class Rotation:
    """视角（度）。"""

    yaw: float = 0.0
    pitch: float = 0.0

    def normalized(self) -> "Rotation":
        return Rotation(wrap_degrees(self.yaw), max(-90.0, min(90.0, self.pitch)))


#: 方块六个面：0=下，1=上，2=北，3=南，4=西，5=东（Minecraft 方向编号）
DIRECTIONS: tuple[BlockPos, ...] = (
    BlockPos(0, -1, 0),
    BlockPos(0, 1, 0),
    BlockPos(0, 0, -1),
    BlockPos(0, 0, 1),
    BlockPos(-1, 0, 0),
    BlockPos(1, 0, 0),
)

DIRECTION_VECTORS: tuple[Vec3, ...] = (
    Vec3(0, -1, 0),
    Vec3(0, 1, 0),
    Vec3(0, 0, -1),
    Vec3(0, 0, 1),
    Vec3(-1, 0, 0),
    Vec3(1, 0, 0),
)


def look_direction(yaw: float, pitch: float) -> Vec3:
    """由 yaw/pitch 求单位视线向量（Minecraft 约定）。"""
    yaw_r = math.radians(yaw)
    pitch_r = math.radians(pitch)
    cos_pitch = math.cos(pitch_r)
    return Vec3(-math.sin(yaw_r) * cos_pitch, -math.sin(pitch_r), math.cos(yaw_r) * cos_pitch)


def look_at(eye: Vec3, target: Vec3) -> Rotation:
    """求从 ``eye`` 看向 ``target`` 的 yaw/pitch。"""
    delta = target - eye
    horizontal = math.hypot(delta.x, delta.z)
    yaw = math.degrees(math.atan2(-delta.x, delta.z))
    pitch = math.degrees(-math.atan2(delta.y, horizontal)) if horizontal > 1e-9 else (90.0 if delta.y < 0 else -90.0)
    return Rotation(wrap_degrees(yaw), max(-90.0, min(90.0, pitch)))


def horizontal_distance(a: Vec3, b: Vec3) -> float:
    return math.hypot(a.x - b.x, a.z - b.z)
