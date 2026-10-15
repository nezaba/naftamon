# Usar este fork como una variante independiente de NaftaMon

Esta guía describe cómo mantener este repositorio como una versión propia, instalarla junto a la aplicación original y seguir publicando cambios en el fork. No requiere abrir un pull request al repositorio original.

## Qué está separado hoy y qué no

El repositorio Git local tiene origin apuntando a siatam87/naftamon. Los commits y git push origin van a ese fork. Eso mantiene separado el código fuente siempre que no se haga push al repositorio original.

La aplicación compilada, en cambio, aún se identifica como naftamon. El actualizador en src/ui.cpp consulta nezaba/naftamon y install.sh escribe en las rutas habituales de NaftaMon. Por lo tanto, cambiar el remote Git no cambia el destino del actualizador ni permite instalar esta compilación en paralelo de forma segura.

## Archivos que se deben adaptar

| Archivo | Qué revisar para la variante |
| --- | --- |
| src/ui.cpp | Cambiar el repositorio constante REPO para consultar el fork. Mantener coherentes la rama consultada (sha=main) y la rama descargada (archive/refs/heads/main.tar.gz). Separar el directorio temporal del actualizador (~/.cache/naftamon-update), el nombre de la carpeta extraída (naftamon-main), el comando de instalación y el comando que espera/reinicia el proceso. Esos valores no deben terminar apuntando a la instalación original. |
| install.sh | Darle a la variante rutas exclusivas para el ejecutable, los datos/icono y el archivo .desktop. Actualizar también los nombres usados para exportar y borrar el icono, pkill/pgrep, los mensajes y la desinstalación. La opción --uninstall debe borrar solo esos recursos y nunca el binario o acceso directo original. |
| src/main.cpp | Dar a la variante un applicationName y desktopFileName propios, por ejemplo naftamon-alt. Esto separa la ubicación que Qt usa para configuración y hace que la aplicación aparezca con su identidad propia. Conviene ajustar también el prefijo del archivo de bloqueo y el mensaje de diagnóstico. |
| src/ui.cpp — AppConfig::path() | La configuración se construye con QStandardPaths::AppConfigLocation y el nombre naftamon.ini. Al cambiar el nombre de aplicación en src/main.cpp, confirmar la ruta efectiva en el sistema de destino. Si se quiere una identidad clara, cambiar también el nombre del INI. No reutilizar ni borrar el archivo de configuración original. |
| naftamon.pro | Cambiar TARGET si se quiere generar un ejecutable distinto, y revisar que install.sh use ese nombre tanto al instalar como al exportar el icono. La versión se marca con el último commit que modificó src/; el actualizador compara esa marca con los commits de la rama configurada. |
| README.md | Actualizar instrucciones de compilación, instalación, nombre del programa, ubicación de configuración y comportamiento del actualizador para que describan la variante y sus rutas. |
| tests/ y laboratorio Docker | Añadir validaciones para el destino del actualizador, la configuración aislada, la instalación/desinstalación sin tocar archivos de NaftaMon original y el funcionamiento simultáneo de ambas identidades. |

Los nombres son ejemplos. La regla clave es que la identidad y todas las rutas de instalación, configuración, caché y reinicio deben ser consistentes y exclusivas de la variante.

## Flujo Git para seguir desarrollando en el fork

Verificar los remotes:

~~~
git remote -v
~~~

En este checkout, origin debe ser el fork propio. Se puede agregar el repositorio original como remote de solo lectura para traer cambios cuando se necesite:

~~~
git remote add upstream git@github.com:nezaba/naftamon.git
git fetch upstream
~~~

Desarrollar commits en una rama propia y publicarlos en el fork:

~~~
git switch -c mi-cambio
# editar y probar
git add .
git commit -m "Describe el cambio"
git push -u origin mi-cambio
~~~

No hace falta crear un pull request al original. Para incorporar cambios del original, revisar upstream/main, integrarlos localmente y luego publicar la rama resultante en origin. No ejecutar git push upstream si no se desea publicar allí.

## Secuencia recomendada para la instalación paralela

1. Elegir una identidad estable para la variante, por ejemplo naftamon-alt.
2. Adaptar todos los puntos de la tabla antes de ejecutar el instalador. No basta con cambiar REPO: el instalador y el reinicio también deben apuntar a la variante.
3. Mantener la rama que consulta el actualizador igual a la que se publica en el fork (actualmente se usa main).
4. Construir y probar primero dentro del laboratorio Docker.
5. Instalar en una cuenta de prueba y confirmar las rutas reales de configuración, binario, icono y acceso directo.
6. Abrir ambas aplicaciones a la vez, verificar que mantienen preferencias separadas y que cada actualizador instala/reinicia solo su propia copia.
7. Probar la desinstalación de la variante y comprobar que la instalación original sigue presente.

La instalación de producción recomendada para el original no debe modificarse durante estas pruebas. Si ya existe una instalación del fork con las rutas antiguas, identificarla antes de probar: puede compartir archivos con NaftaMon original.

## Comprobaciones antes de publicar una versión

- Buscar referencias residuales a nezaba/naftamon, a la ruta de instalación original y a los nombres de proceso originales.
- Confirmar que consulta y descarga desde el fork y la rama correcta.
- Ejecutar compilación y pruebas con docker compose -f compose.dev.yaml.
- Probar los flujos de instalación, actualización y desinstalación en un entorno Linux de prueba con ambas variantes instaladas.
- No declarar seguro el modo paralelo hasta probar la instalación y actualización reales. El laboratorio Docker valida la compilación y el comportamiento de la aplicación, pero no reemplaza una prueba del instalador en el escritorio Linux de destino.

## Alcance de la plataforma

El actualizador y install.sh de este repositorio descargan archivos con curl/wget, compilan con qmake6/make y usan rutas y utilidades de Linux. Docker Desktop en Windows sirve para validar el laboratorio Linux, pero no convierte este instalador en un instalador nativo de Windows.
