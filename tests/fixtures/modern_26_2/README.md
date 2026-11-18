# Official Java 26.2 chunk fixture

`sections.bin` (2772 bytes) was produced by the bundled `WriteChunkFixture.java`
using the official unobfuscated server 26.2, JDK 25.0.2 and all official bundle
library jars on the classpath. This invokes **LevelChunkSection.write**, not a
Python encoder. No running server, EULA or world files are needed.

SHA256: `e53d1a45b11c312ecde17fb62fcf669306c18b53ec0ee8ef9de792ad4779424a`

Two bottom-up sections: first 4096 values selected by `(index*7+3)%19`, where
0 is air, 18 is waterlogged default oak slab (official state 13332), and values
1..17 are official block states 1..17. The resulting official block palette is
5 bits, so long packing includes unused high padding bits. The second section
is singleton air. Both biome containers are singleton plains, with a local
one-entry fixture registry (plains ID 0), not the actual server biome ID order.
Tests assert all 8192 state values and both block/fluid counts.

Reproduce from the workspace root in PowerShell (official research files are
intentionally NOT distributed with the plugin):

```powershell
$jars = (Get-ChildItem research-26.2/bundle -Recurse -Filter *.jar).FullName -join ';'
& research-26.2/jdk-25.0.2/bin/javac.exe -cp $jars maibot-minecraft/tests/fixtures/modern_26_2/WriteChunkFixture.java
& research-26.2/jdk-25.0.2/bin/java.exe -cp "maibot-minecraft/tests/fixtures/modern_26_2;$jars" WriteChunkFixture maibot-minecraft/tests/fixtures/modern_26_2/sections.bin
```

This fixture validates vanilla serialization, not a live network login/chunk
capture. Collision data elsewhere is queried at EmptyBlockGetter/BlockPos.ZERO
with no entity collision context; context-sensitive behavior remains limited.
