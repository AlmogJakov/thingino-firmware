INGENIC_UCLIBC_VERSION = 97c9ba8
INGENIC_UCLIBC_SITE = https://github.com/gtxaspec/ingenic-uclibc
INGENIC_UCLIBC_SITE_METHOD = git
INGENIC_UCLIBC_INSTALL_STAGING = YES

INGENIC_UCLIBC_LICENSE = MIT
INGENIC_UCLIBC_LICENSE_FILES = LICENSE

# NOTE: -flto removed. libuclibcshim.so is a hand-written glibc/musl->uClibc ABI
# bridge (uclibc_shim.c) that pokes raw FILE-struct offsets and interposes libc
# symbols. Building it with -flto on the buildroot 2026.02.1 gcc miscompiled it
# (SIGSEGV inside the shim's __fputc_unlocked at startup -> every prudynt crashed;
# good shim sha 07710f80 from the older toolchain, bad sha b3ead471 from new+LTO).
# LTO buys ~nothing on a single 1-file .so but breaks this ABI-sensitive code, so
# it is intentionally OFF here. Do NOT re-add -flto without verifying prudynt boots.
INGENIC_UCLIBC_CFLAGS = -Os -ffunction-sections -fdata-sections \
	-fno-asynchronous-unwind-tables -fmerge-all-constants -fno-ident

define INGENIC_UCLIBC_BUILD_CMDS
	$(TARGET_CC) $(INGENIC_UCLIBC_CFLAGS) -fPIC -shared -o $(@D)/libuclibcshim.so $(@D)/uclibc_shim.c
	$(TARGET_CC) $(INGENIC_UCLIBC_CFLAGS) -c -o $(@D)/uclibc_shim.o $(@D)/uclibc_shim.c
	$(TARGET_CROSS)gcc-ar rcs $(@D)/libuclibcshim.a $(@D)/uclibc_shim.o
endef

define INGENIC_UCLIBC_INSTALL_STAGING_CMDS
	$(INSTALL) -D -m 0755 $(@D)/libuclibcshim.so $(STAGING_DIR)/usr/lib/libuclibcshim.so
	$(INSTALL) -D -m 0644 $(@D)/libuclibcshim.a $(STAGING_DIR)/usr/lib/libuclibcshim.a
endef

# Raptor links the shim statically — skip .so on device when raptor is the streamer
ifneq ($(BR2_PACKAGE_THINGINO_RAPTOR),y)
define INGENIC_UCLIBC_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/libuclibcshim.so $(TARGET_DIR)/usr/lib/libuclibcshim.so
endef
endif

$(eval $(generic-package))
