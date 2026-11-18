import net.minecraft.SharedConstants;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.block.*;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.BlockStateProperties;
import net.minecraft.world.level.chunk.*;
import net.minecraft.core.*;
import net.minecraft.world.level.biome.*;
import net.minecraft.network.FriendlyByteBuf;
import io.netty.buffer.Unpooled;
import java.nio.file.*;
public class WriteChunkFixture {
 public static void main(String[] args) throws Exception {
  SharedConstants.tryDetectVersion();Bootstrap.bootStrap();
  var lookup=net.minecraft.data.registries.VanillaRegistries.createLookup();
  var plains=lookup.lookupOrThrow(net.minecraft.core.registries.Registries.BIOME).getOrThrow(Biomes.PLAINS);
  IdMapper<Holder<Biome>> map=new IdMapper<>();map.add(plains);
  var biomes=new PalettedContainer<Holder<Biome>>(plains,Strategy.createForBiomes(map));
  var blocks=new PalettedContainer<BlockState>(Blocks.AIR.defaultBlockState(),Strategy.createForBlockStates(Block.BLOCK_STATE_REGISTRY));
  var waterlogged=Blocks.OAK_SLAB.defaultBlockState().setValue(BlockStateProperties.WATERLOGGED,true);
  for(int i=0;i<4096;i++) {
   int value=(i*7+3)%19;
   BlockState state=value==0?Blocks.AIR.defaultBlockState():value==18?waterlogged:Block.stateById(value);
   blocks.set(i&15,i>>8,(i>>4)&15,state);
  }
  var section=new LevelChunkSection(blocks,biomes);section.recalcBlockCounts();
  var air=new LevelChunkSection(new PalettedContainer<BlockState>(Blocks.AIR.defaultBlockState(),Strategy.createForBlockStates(Block.BLOCK_STATE_REGISTRY)),biomes);
  var buf=new FriendlyByteBuf(Unpooled.buffer());section.write(buf);air.write(buf);
  byte[] data=new byte[buf.readableBytes()];buf.readBytes(data);Files.write(Path.of(args[0]),data);
  System.out.println("waterlogged="+Block.getId(waterlogged)+" bits="+blocks.bitsPerEntry()+" bytes="+data.length);
 }
}
