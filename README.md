# f04_to_f76_texture_converter
Auto converts Fallout 4 textures to Fallout 76 textures using python.



Requirements:

Make sure you have python installed, and you'll need both pillow and texfury to run it. Once you have python installed, install both with the following pip command:

pip install texfury pillow

Texfury - used for BC7 encoding
Pillow - used for processing images



Naming scheme:

The Fallout 4 textures need to use the following naming scheme. If the files aren't named correctly, the script will tell you that it couldn't find the files:

Normal - name needs to end with _n
Diffuse - name needs to end with _d
Specular - name needs to end with _s

Example names:
MyF04Mod_n.dds
MyF04Mod_d.dds
MyF04Mod_s.dds



How to run the file:

Once you have the Fallout 4 textures named correctly, and texfury and pillow installed, run the following command:

python "directory/to/Convert f04 to f76.py" -s "Fallout 4 Texture Folder" -o "Fallout 76 Texture Folder"

Important: 

Replace the " directory/to/Convert f04 to f76.py " section with the directory to the python file, wherever you downloaded it to
The -s parameter is source parameter which should point to the folder where the Fallout 4 textures are located
The -o parameter is the output folder which should point to the folder where the Fallout 76 textures will be saved


The Fallout 76 textures should be saved to the output directory. From there, you just need to create the material using Material Editor, and then point any meshes to it using Outfit Studio. 
