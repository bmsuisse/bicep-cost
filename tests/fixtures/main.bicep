param skuName string = 'P1v3'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan'
  location: 'westeurope'
  kind: 'linux'
  sku: {
    name: skuName
    capacity: 2
  }
  properties: {
    reserved: true
  }
}
