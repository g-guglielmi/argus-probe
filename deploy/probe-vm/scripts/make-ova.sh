#!/usr/bin/env bash
# Package the golden qcow2 as an OVA (VMware / Nutanix / VirtualBox; also importable by Xen Orchestra
# as a full VM, not just a bare VDI). An OVA is a plain tar of three members: the OVF descriptor, a
# SHA256 manifest, and the disk as a stream-optimized VMDK. No proprietary tooling - qemu-img makes
# the VMDK, we hand-write the OVF.
#
# Usage: make-ova.sh <input.qcow2> <output-dir> [vm-name]
set -euo pipefail

SRC="${1:?usage: make-ova.sh <input.qcow2> <output-dir> [vm-name]}"
OUTDIR="${2:?output dir required}"
NAME="${3:-argus-probe-vm}"

VMDK="$OUTDIR/$NAME.vmdk"
OVF="$OUTDIR/$NAME.ovf"
OVA="$OUTDIR/$NAME.ova"

CPUS=2
MEM_MB=2048

mkdir -p "$OUTDIR"

echo "==> converting qcow2 -> stream-optimized VMDK (lsilogic SCSI)"
# streamOptimized = the compressed monolithic VMDK OVAs use; adapter_type matches the SCSI controller
# declared in the OVF below so strict importers (VMware) don't see a controller/disk mismatch.
qemu-img convert -f qcow2 -O vmdk -o subformat=streamOptimized,adapter_type=lsilogic "$SRC" "$VMDK"

# Virtual capacity (bytes) = the disk's logical size, read from the VMDK's own geometry. Parse the
# human-readable "virtual size: N GiB (<bytes> bytes)" line (exactly one parenthesised byte count) -
# robust across qemu versions, unlike the JSON field order. The archived VMDK file size comes from stat.
qemu-img info "$VMDK"
CAPACITY=$(qemu-img info "$VMDK" | sed -n 's/.*(\([0-9][0-9]*\) bytes).*/\1/p' | head -n1)
FILESIZE=$(stat -c%s "$VMDK")
: "${CAPACITY:?could not read the disk virtual size}"

echo "==> writing OVF descriptor (capacity=$CAPACITY bytes, vmdk=$FILESIZE bytes)"
cat > "$OVF" <<OVF_EOF
<?xml version="1.0" encoding="UTF-8"?>
<Envelope ovf:version="1.0" xml:lang="en-US"
    xmlns="http://schemas.dmtf.org/ovf/envelope/1"
    xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
    xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
    xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData">
  <References>
    <File ovf:href="$NAME.vmdk" ovf:id="file1" ovf:size="$FILESIZE"/>
  </References>
  <DiskSection>
    <Info>Virtual disk information</Info>
    <Disk ovf:capacity="$CAPACITY" ovf:diskId="vmdisk1" ovf:fileRef="file1" ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
  </DiskSection>
  <NetworkSection>
    <Info>The list of logical networks</Info>
    <Network ovf:name="nat">
      <Description>The nat network</Description>
    </Network>
  </NetworkSection>
  <VirtualSystem ovf:id="$NAME">
    <Info>Argus probe golden image</Info>
    <Name>$NAME</Name>
    <OperatingSystemSection ovf:id="96" ovf:version="13">
      <Info>The operating system installed</Info>
      <Description>Debian GNU/Linux 13 (64-bit)</Description>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware requirements</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>$NAME</vssd:VirtualSystemIdentifier>
        <vssd:VirtualSystemType>vmx-11</vssd:VirtualSystemType>
      </System>
      <Item>
        <rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>
        <rasd:Description>Number of Virtual CPUs</rasd:Description>
        <rasd:ElementName>$CPUS virtual CPU(s)</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>$CPUS</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
        <rasd:Description>Memory Size</rasd:Description>
        <rasd:ElementName>$MEM_MB MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>$MEM_MB</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Address>0</rasd:Address>
        <rasd:Description>SCSI Controller</rasd:Description>
        <rasd:ElementName>scsiController0</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>lsilogic</rasd:ResourceSubType>
        <rasd:ResourceType>6</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:ElementName>disk1</rasd:ElementName>
        <rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
        <rasd:Connection>nat</rasd:Connection>
        <rasd:Description>E1000 ethernet adapter on "nat"</rasd:Description>
        <rasd:ElementName>ethernet0</rasd:ElementName>
        <rasd:InstanceID>5</rasd:InstanceID>
        <rasd:ResourceSubType>E1000</rasd:ResourceSubType>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
OVF_EOF

echo "==> writing manifest (SHA256)"
( cd "$OUTDIR" && {
    printf 'SHA256(%s)= %s\n' "$NAME.ovf"  "$(sha256sum "$NAME.ovf"  | cut -d' ' -f1)"
    printf 'SHA256(%s)= %s\n' "$NAME.vmdk" "$(sha256sum "$NAME.vmdk" | cut -d' ' -f1)"
  } > "$NAME.mf" )

echo "==> assembling OVA (tar order: ovf, mf, vmdk)"
# The OVF spec requires the descriptor first in the archive and the disk last; ustar for the broadest
# importer compatibility. Plain (uncompressed) tar - the VMDK is already stream-compressed.
( cd "$OUTDIR" && tar --format=ustar -cf "$NAME.ova" "$NAME.ovf" "$NAME.mf" "$NAME.vmdk" )

echo "==> validating"
xmllint --noout "$OVF"
tar -tf "$OVA"
ls -lh "$OVA"
echo "==> OVA ready: $OVA"
