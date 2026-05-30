import LogoMark from "@/components/LogoMark"

function BrandHeading({ size = 40, className = "" }) {
  return (
    <div className={`flex items-center gap-3 ${className}`}>
      <LogoMark size={size} className="shrink-0" />
      <h1 className="text-2xl font-semibold tracking-tight">Xcash</h1>
    </div>
  )
}

export default BrandHeading
