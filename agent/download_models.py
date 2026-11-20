from argostranslate import package

WANTED = {("en", "zh"), ("zh", "en")}

package.update_package_index()
for pkg in package.get_available_packages():
    if (pkg.from_code, pkg.to_code) in WANTED:
        print(f"Downloading {pkg} ...")
        package.install_from_path(pkg.download())
        print(f"Installed {pkg}")

print("Done. Installed languages:")
from argostranslate import translate

for lang in translate.get_installed_languages():
    print(f"  {lang.code}: {lang.name}")
