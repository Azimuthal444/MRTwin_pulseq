function waveform = pts2waveform(times, amplitudes, gradRasterTime)

grd = min(times):gradRasterTime:max(times);
grd = grd(1:end-1);
waveform = interp1(times, amplitudes, grd + gradRasterTime/2);

% % times = ceil(times/gradRasterTime)*gradRasterTime; % round onto grid
% times = ceil(times/gradRasterTime); % round onto grid. SK: Dont multiply by 
%                                     % gradRasterTime here: This will 
%                                     % introduce numerical inaccuracies.
% times_diff = diff(times);
% amplitudes_diff = diff(amplitudes);
% waveform = [];
% for ii = 1:length(times)-1
%     % SK: there are no new points after the end, therefore we dont need to
%     % handle the overlap situation.
%     if ii == length(times)-1
%         crop = 0;
%     else
%         crop = 1;
%     end
%     y = amplitudes_diff(ii)/times_diff(ii)*...
%         (0:1:(times(ii+1)-times(ii)-crop))...
%         + amplitudes(ii);
%     waveform = [waveform y(1:end)];
% end
end