Future<String> mapTilesSourceUrl() async {
  // Flutter exposes declared assets below assets/assets/ in a web build. Use
  // the current origin so this also works on localhost and a future domain.
  final assetUrl = Uri.base.resolve('assets/assets/tiles/klang_valley.pmtiles');
  return 'pmtiles://$assetUrl';
}
