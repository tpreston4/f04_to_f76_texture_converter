import os
import argparse
from PIL import Image
from texfury import Texture, BCFormat

def convert_textures_to_bc7(source_dir, output_dir):
    # Verify source directory exists
    if not os.path.exists(source_dir):
        print(f"Error: Source directory '{source_dir}' does not exist.")
        return

    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")

    files = os.listdir(source_dir)
    specular_maps = [f for f in files if f.lower().endswith('_s.dds')]

    if not specular_maps:
        print(f"No Fallout 4 specular maps (_s.dds) found in: {source_dir}")
        return

    print(f"Starting BC7 Texture Conversion for {len(specular_maps)} sets...\n")

    for spec_file in specular_maps:
        base_name = spec_file[:-6]
        print(f"Processing & Compressing: {base_name}")

        # Source paths
        fo4_spec_path = os.path.join(source_dir, spec_file)
        fo4_diff_path = os.path.join(source_dir, f"{base_name}_d.dds")
        fo4_norm_path = os.path.join(source_dir, f"{base_name}_n.dds")

        # Destination paths
        fo76_albedo_path = os.path.join(output_dir, f"{base_name}_d.dds")
        fo76_normal_path = os.path.join(output_dir, f"{base_name}_n.dds")
        fo76_light_path = os.path.join(output_dir, f"{base_name}_l.dds")
        fo76_reflec_path = os.path.join(output_dir, f"{base_name}_r.dds")

        # Process and Compress Specular maps to _l and _r
        if os.path.exists(fo4_spec_path):
            with Image.open(fo4_spec_path) as img_spec:
                img_spec = img_spec.convert('RGBA')
                r_fo4, g_fo4, b_fo4, a_fo4 = img_spec.split()

                # Build FO76 Lighting Map (Red = Smoothness, Green = AO, Blue = Cavity, Alpha = Emissive)
                white_channel = Image.new('L', img_spec.size, 255)
                black_alpha = Image.new('L', img_spec.size, 0) # Pure black tells the engine: NO GLOW

                # Merge as RGBA instead of RGB to lock down the emissive channel
                img_light = Image.merge('RGBA', (g_fo4, white_channel, white_channel, black_alpha))

                # Compress & Save _l map to BC7 (BC7 cleanly supports the alpha channel)
                tex_light = Texture.from_pil(img_light, format=BCFormat.BC7, quality=0.8)
                tex_light.save_dds(fo76_light_path)

                # Build FO76 Reflectance Map (Grayscale metalness baseline)
                img_reflec = Image.merge('RGB', (r_fo4, r_fo4, r_fo4))
                
                # Compress & Save _r map to BC7
                tex_reflec = Texture.from_pil(img_reflec, format=BCFormat.BC7, quality=0.8)
                tex_reflec.save_dds(fo76_reflec_path)

        # Compress Albedo/Diffuse map to BC7
        if os.path.exists(fo4_diff_path):
            with Image.open(fo4_diff_path) as img_diff:
                tex_diff = Texture.from_pil(img_diff, format=BCFormat.BC7, quality=0.8)
                tex_diff.save_dds(fo76_albedo_path)

        # Compress Normal map to BC7
        if os.path.exists(fo4_norm_path):
            with Image.open(fo4_norm_path) as img_norm:
                tex_norm = Texture.from_pil(img_norm, format=BCFormat.BC7, quality=0.8)
                tex_norm.save_dds(fo76_normal_path)

    print("\nAll textures successfully packed and compressed to BC7 DDS format!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert Fallout 4 texture maps to Fallout 76 BC7 compressed DDS textures.")
    
    # Define arguments
    parser.add_argument("-s", "--source", required=True, help="Path to the directory containing Fallout 4 textures.")
    parser.add_argument("-o", "--output", required=True, help="Path to the directory where converted Fallout 76 textures will be saved.")
    
    args = parser.parse_args()

    # Pass command line arguments into the converter function
    convert_textures_to_bc7(args.source, args.output)